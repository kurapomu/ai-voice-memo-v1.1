"""
Zoom Server-to-Server OAuth クライアント。

VPS 配置先: /opt/jizo-api/zoom_client.py
（ローカル正典: _zoom_client_remote.py → scp で配置）

- account_credentials grant でアクセストークンを取得
- 有効期限内（残り60秒以上）はトークンをキャッシュして再利用
- 録画ファイルのストリーミング DL（download_token または OAuth Bearer）
"""
import time
import base64
import httpx


class ZoomClient:
    TOKEN_URL = "https://zoom.us/oauth/token"
    API_BASE = "https://api.zoom.us/v2"

    def __init__(self, client_id: str, client_secret: str, account_id: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get_access_token(self) -> str:
        """アクセストークンを返す。残り60秒以下になったら再取得"""
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(
                self.TOKEN_URL,
                params={
                    "grant_type": "account_credentials",
                    "account_id": self.account_id,
                },
                headers={"Authorization": f"Basic {basic}"},
            )
            r.raise_for_status()
            data = r.json()

        self._token = data["access_token"]
        self._expires_at = now + int(data.get("expires_in", 3600))
        return self._token

    async def download_file(
        self, url: str, dest_path: str, token: str | None = None
    ) -> None:
        """録画ファイルをストリーミングDLする。

        - `token` が指定されていれば Zoom の `download_token`（クエリ）を使用
        - 未指定なら OAuth Bearer トークンを使用
        """
        params = {"access_token": token} if token else None
        headers = None if token else {
            "Authorization": f"Bearer {await self.get_access_token()}"
        }
        async with httpx.AsyncClient(
            timeout=300, follow_redirects=True
        ) as cli:
            async with cli.stream(
                "GET", url, params=params, headers=headers
            ) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(64 * 1024):
                        f.write(chunk)
