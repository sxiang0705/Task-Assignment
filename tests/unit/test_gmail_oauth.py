from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from task_assignment.infrastructure.gmail import (
    GMAIL_SEND_SCOPE,
    OAUTH_SCOPES,
    GmailApiClient,
    GmailAuthorizationError,
    GmailMessageError,
    GoogleOAuthClient,
    LoopbackAuthorizationReceiver,
    OAuthClientConfig,
    OAuthTokens,
    build_authorization_url,
    parse_authorization_callback,
)


@pytest.mark.parametrize("payload", [
    None, [], {"web": {}}, {"installed": {}},
    {"installed": {"client_id": 123, "client_secret": "test"}},
    {"installed": {"client_id": "test.apps.googleusercontent.com",
                   "client_secret": "test", "access_token": "forbidden"}},
])
def test_publisher_config_rejects_wrong_type_and_user_credentials(payload):
    with pytest.raises(ValueError):
        OAuthClientConfig.from_payload(payload)


def test_authorization_url_uses_loopback_state_pkce_offline_and_minimum_scopes() -> None:
    url = build_authorization_url(
        OAuthClientConfig("desktop-client.apps.googleusercontent.com"),
        redirect_uri="http://127.0.0.1:54321/oauth2/callback",
        state="unpredictable-state",
        code_challenge="pkce-challenge",
    )
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert query["redirect_uri"] == ["http://127.0.0.1:54321/oauth2/callback"]
    assert query["state"] == ["unpredictable-state"]
    assert query["code_challenge"] == ["pkce-challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert set(query["scope"][0].split()) == set(OAUTH_SCOPES)
    assert GMAIL_SEND_SCOPE in query["scope"][0]
    assert not any("readonly" in scope or "modify" in scope for scope in OAUTH_SCOPES)


def test_callback_requires_exact_state_and_rejects_errors() -> None:
    callback = "/oauth2/callback?code=one-time-code&state=expected"
    assert parse_authorization_callback(callback, "expected") == "one-time-code"

    with pytest.raises(GmailAuthorizationError, match="安全驗證失敗"):
        parse_authorization_callback(callback, "different")
    with pytest.raises(GmailAuthorizationError, match="取消|拒絕"):
        parse_authorization_callback(
            "/oauth2/callback?error=access_denied&state=expected", "expected"
        )
    with pytest.raises(GmailAuthorizationError, match="不完整"):
        parse_authorization_callback(
            "/oauth2/callback?code=first&code=second&state=expected", "expected"
        )
    with pytest.raises(GmailAuthorizationError, match="格式不正確"):
        parse_authorization_callback("/oauth2/callback?broken&state=expected", "expected")


def test_loopback_receiver_rejects_replayed_callback(monkeypatch) -> None:
    receiver = LoopbackAuthorizationReceiver()
    receiver.server.callback_path = "/oauth2/callback?code=one-time&state=expected"
    monkeypatch.setattr(receiver.server, "handle_request", lambda: None)
    try:
        assert receiver.wait_for_code("expected", 1) == "one-time"
        with pytest.raises(GmailAuthorizationError, match="已使用"):
            receiver.wait_for_code("expected", 1)
    finally:
        receiver.close()


def test_token_value_objects_do_not_expose_tokens_in_repr() -> None:
    tokens = OAuthTokens(
        access_token="access-secret",
        refresh_token="refresh-secret",
        account_email="person@example.com",
    )

    rendered = repr(tokens)

    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "person@example.com" in rendered

    config = OAuthClientConfig("desktop-client", "client-secret")
    assert "client-secret" not in repr(config)


class _FakeHttp:
    def __init__(self) -> None:
        self.url = ""
        self.method = ""
        self.data = b""
        self.headers: dict[str, str] = {}

    def request_json(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, str]:
        del timeout
        self.url = url
        self.method = method
        self.data = data or b""
        self.headers = headers or {}
        return {"id": "gmail-message-id"}


def test_gmail_client_builds_base64url_mime_with_zip_attachment(tmp_path: Path) -> None:
    backup = tmp_path / "task-assignment-backup.zip"
    backup.write_bytes(b"PK\x03\x04 verified backup")
    http = _FakeHttp()

    message_id = GmailApiClient(http).send(
        access_token="memory-only-access-token",
        sender="sender@example.com",
        recipient="recipient@example.net",
        subject="Task Assignment backup",
        body="Backup body",
        attachment=backup,
    )

    assert message_id == "gmail-message-id"
    assert http.method == "POST"
    assert http.url.endswith("/gmail/v1/users/me/messages/send")
    assert http.headers["Authorization"] == "Bearer memory-only-access-token"
    encoded = json.loads(http.data)["raw"]
    encoded += "=" * (-len(encoded) % 4)
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded))
    assert message["From"] == "sender@example.com"
    assert message["To"] == "recipient@example.net"
    assert message["Subject"] == "Task Assignment backup"
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == backup.name
    assert attachment.get_content() == backup.read_bytes()


def test_gmail_client_rejects_oversized_attachment_before_network(tmp_path: Path) -> None:
    backup = tmp_path / "oversized.zip"
    with backup.open("wb") as stream:
        stream.seek(24 * 1024 * 1024)
        stream.write(b"x")
    http = _FakeHttp()

    with pytest.raises(GmailMessageError, match="24 MiB"):
        GmailApiClient(http).send(
            access_token="access",
            sender="sender@example.com",
            recipient="recipient@example.net",
            subject="Backup",
            body="Body",
            attachment=backup,
        )

    assert http.url == ""


class _FakeReceiver:
    redirect_uri = "http://127.0.0.1:45678/oauth2/callback"

    def __init__(self) -> None:
        self.expected_state = ""
        self.closed = False

    def wait_for_code(self, expected_state: str, timeout: float) -> str:
        assert timeout > 0
        self.expected_state = expected_state
        return "authorization-code"

    def close(self) -> None:
        self.closed = True


class _FakeOAuthHttp:
    def __init__(self) -> None:
        self.token_fields: dict[str, str] = {}

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        assert url.endswith("/token")
        self.token_fields = fields
        return {
            "access_token": "short-lived-access",
            "refresh_token": "long-lived-refresh",
            "expires_in": 3600,
        }

    def request_json(self, *_args, **_kwargs) -> dict[str, object]:
        return {"email": "owner@example.com", "email_verified": True}


def test_google_oauth_generated_pkce_matches_token_exchange() -> None:
    receiver = _FakeReceiver()
    http = _FakeOAuthHttp()
    opened_urls: list[str] = []
    client = GoogleOAuthClient(
        OAuthClientConfig("desktop.apps.googleusercontent.com"),
        http=http,
        browser_opener=lambda url: not opened_urls.append(url),
        receiver_factory=lambda: receiver,
    )

    tokens = client.authorize()

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened_urls[0]).query)
    verifier = http.token_fields["code_verifier"]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert query["code_challenge"] == [expected_challenge]
    assert query["state"] == [receiver.expected_state]
    assert http.token_fields["redirect_uri"] == receiver.redirect_uri
    assert http.token_fields["code"] == "authorization-code"
    assert tokens.account_email == "owner@example.com"
    assert receiver.closed is True
