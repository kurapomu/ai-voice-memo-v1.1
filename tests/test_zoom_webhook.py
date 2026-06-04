"""
Zoom Webhook 署名検証ユーティリティのテスト。
- HMAC-SHA256 で v0=... 形式の署名を検証する
- URL Validation Challenge 応答（plainToken + encryptedToken）を生成する
"""
import hmac
import hashlib


def test_verify_signature_valid():
    from zoom_webhook import verify_signature

    secret = "mysecret"
    body = b'{"event":"recording.completed"}'
    ts = "1700000000"
    msg = f"v0:{ts}:{body.decode()}"
    sig = "v0=" + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    assert verify_signature(body, ts, sig, secret) is True


def test_verify_signature_invalid():
    from zoom_webhook import verify_signature

    assert verify_signature(b"{}", "1700000000", "v0=bad", "mysecret") is False


def test_verify_signature_missing_signature():
    from zoom_webhook import verify_signature

    assert verify_signature(b"{}", "1700000000", "", "mysecret") is False


def test_verify_signature_none_signature():
    from zoom_webhook import verify_signature

    assert verify_signature(b"{}", "1700000000", None, "mysecret") is False


def test_url_validation_response():
    from zoom_webhook import build_url_validation_response

    secret = "mysecret"
    plain = "abc123"
    resp = build_url_validation_response(plain, secret)

    assert resp["plainToken"] == plain
    expected = hmac.new(secret.encode(), plain.encode(), hashlib.sha256).hexdigest()
    assert resp["encryptedToken"] == expected
