"""
ZoomClient（Server-to-Server OAuth）のテスト。
- アクセストークン取得とキャッシュ動作（expires_in 内は再取得しない）
- ダウンロードの基本動作
"""
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_token_response(token: str = "tok123", expires_in: int = 3600):
    """httpx.AsyncClient.post のモックレスポンスを作る"""
    resp = MagicMock()
    resp.json = MagicMock(return_value={"access_token": token, "expires_in": expires_in})
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


@pytest.mark.asyncio
async def test_get_access_token_returns_token():
    from zoom_client import ZoomClient

    client = ZoomClient(client_id="cid", client_secret="csec", account_id="aid")
    mock_resp = _make_token_response("tok123")

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=mock_resp)):
        token = await client.get_access_token()

    assert token == "tok123"


@pytest.mark.asyncio
async def test_get_access_token_caches_token():
    """有効期限内は再取得しない"""
    from zoom_client import ZoomClient

    client = ZoomClient(client_id="cid", client_secret="csec", account_id="aid")
    mock_resp = _make_token_response("tok123", expires_in=3600)
    mock_post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.post", mock_post):
        t1 = await client.get_access_token()
        t2 = await client.get_access_token()

    assert t1 == "tok123"
    assert t2 == "tok123"
    assert mock_post.call_count == 1   # キャッシュされる


@pytest.mark.asyncio
async def test_get_access_token_refreshes_when_expired():
    """期限切れ間際（残り60秒以下）になったら再取得する"""
    from zoom_client import ZoomClient

    client = ZoomClient(client_id="cid", client_secret="csec", account_id="aid")

    # 最初は expires_in=10 → すぐ期限切れ扱い（残り60秒以下）
    resp1 = _make_token_response("tok-old", expires_in=10)
    resp2 = _make_token_response("tok-new", expires_in=3600)
    mock_post = AsyncMock(side_effect=[resp1, resp2])

    with patch("httpx.AsyncClient.post", mock_post):
        t1 = await client.get_access_token()
        t2 = await client.get_access_token()   # 期限切れ間際なので再取得

    assert t1 == "tok-old"
    assert t2 == "tok-new"
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_get_access_token_uses_basic_auth_header():
    """Authorization: Basic <base64(client_id:client_secret)> が送られる"""
    from zoom_client import ZoomClient

    client = ZoomClient(client_id="myid", client_secret="mysec", account_id="aid")
    mock_resp = _make_token_response()
    mock_post = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient.post", mock_post):
        await client.get_access_token()

    _, kwargs = mock_post.call_args
    # base64("myid:mysec") = bXlpZDpteXNlYw==
    assert kwargs["headers"]["Authorization"] == "Basic bXlpZDpteXNlYw=="
    assert kwargs["params"]["grant_type"] == "account_credentials"
    assert kwargs["params"]["account_id"] == "aid"
