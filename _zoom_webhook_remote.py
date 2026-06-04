"""
Zoom Webhook 署名検証ユーティリティ。

VPS 配置先: /opt/jizo-api/zoom_webhook.py
（ローカル正典: _zoom_webhook_remote.py → scp で配置）

Zoom の Event Notification 仕様:
  - 通常イベント: x-zm-signature ヘッダで HMAC-SHA256 検証
  - 初期検証: event=endpoint.url_validation の plainToken に対して
    HMAC-SHA256 で encryptedToken を返却
"""
import hmac
import hashlib


def verify_signature(body: bytes, timestamp: str, signature: str | None, secret: str) -> bool:
    """Zoom Webhook の HMAC-SHA256 署名を検証する。

    署名形式:
        message  = f"v0:{timestamp}:{body}"
        expected = "v0=" + HMAC-SHA256(secret, message).hex()
        verify   = (expected == x-zm-signature)
    """
    if not signature:
        return False
    msg = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        secret.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def build_url_validation_response(plain_token: str, secret: str) -> dict:
    """Zoom Marketplace の Webhook URL 初期検証への応答を生成する。

    Zoom から `event=endpoint.url_validation` で plainToken が飛んできたら、
    そのトークンを HMAC-SHA256 で暗号化したものを encryptedToken として返す。
    """
    encrypted = hmac.new(
        secret.encode(), plain_token.encode(), hashlib.sha256
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted}
