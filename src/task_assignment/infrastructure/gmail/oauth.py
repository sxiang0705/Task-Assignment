"""Google OAuth desktop flow with loopback callback, state, and S256 PKCE."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol

from task_assignment.infrastructure.gmail.contracts import (
    GmailAuthorizationError,
    GmailConfigurationError,
    GmailNetworkError,
    GmailReauthorizationRequired,
    OAuthTokens,
)

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USER_INFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
OAUTH_SCOPES = ("openid", EMAIL_SCOPE, GMAIL_SEND_SCOPE)
OAUTH_CONFIG_ENV = "TASK_ASSIGNMENT_GOOGLE_CLIENT_ID"
OAUTH_SECRET_ENV = "TASK_ASSIGNMENT_GOOGLE_CLIENT_SECRET"
CALLBACK_TIMEOUT_SECONDS = 180


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str = field(default="", repr=False)

    @classmethod
    def from_payload(cls, payload: object) -> OAuthClientConfig:
        """Accept only Google's Desktop client export, never user credentials."""
        if not isinstance(payload, dict) or set(payload) != {"installed"}:
            raise ValueError("需要 Google Desktop app 設定檔。")
        installed = payload["installed"]
        allowed = {
            "client_id", "client_secret", "project_id", "auth_uri", "token_uri",
            "auth_provider_x509_cert_url", "redirect_uris",
        }
        if not isinstance(installed, dict) or set(installed) - allowed:
            raise ValueError("Google 設定含有不支援的欄位。")
        client_id = installed.get("client_id")
        secret = installed.get("client_secret")
        if (
            not isinstance(client_id, str)
            or not client_id.endswith(".apps.googleusercontent.com")
            or any(char.isspace() for char in client_id)
            or not isinstance(secret, str)
            or not secret.strip()
        ):
            raise ValueError("Google Desktop app 設定不完整。")
        return cls(client_id, secret)

    @classmethod
    def load(cls) -> OAuthClientConfig | None:
        client_id = os.environ.get(OAUTH_CONFIG_ENV, "").strip()
        if client_id:
            return cls(client_id, os.environ.get(OAUTH_SECRET_ENV, "").strip())
        for path in _oauth_config_paths():
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return cls.from_payload(payload)
            except (OSError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return None


class JsonHttpClient:
    def request_json(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            error_code = _remote_error_code(exc)
            raise _HttpResponseError(exc.code, error_code) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise GmailNetworkError("無法連線至 Google，請檢查網路後再試。") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailNetworkError("Google 回傳了無法辨識的資料。") from exc
        if not isinstance(value, dict):
            raise GmailNetworkError("Google 回傳了不完整的資料。")
        return value

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        return self.request_json(
            url,
            method="POST",
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def post_form_without_response(self, url: str, fields: dict[str, str]) -> None:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as exc:
            raise _HttpResponseError(exc.code, _remote_error_code(exc)) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise GmailNetworkError("無法連線至 Google，請檢查網路後再試。") from exc


class _HttpResponseError(RuntimeError):
    def __init__(self, status: int, error_code: str | None) -> None:
        super().__init__("Google request failed")
        self.status = status
        self.error_code = error_code


class AuthorizationReceiver(Protocol):
    redirect_uri: str

    def wait_for_code(self, expected_state: str, timeout: float) -> str: ...

    def close(self) -> None: ...


class _CallbackServer(HTTPServer):
    callback_path: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        server = self.server
        if isinstance(server, _CallbackServer):
            server.callback_path = self.path
        message = (
            "<!doctype html><meta charset='utf-8'><title>Task Assignment</title>"
            "<h1>Task Assignment</h1><p>已收到 Gmail 授權結果，可以關閉此頁面。</p>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        self.wfile.write(message)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LoopbackAuthorizationReceiver:
    def __init__(self) -> None:
        self.server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}/oauth2/callback"
        self._consumed = False

    def wait_for_code(self, expected_state: str, timeout: float) -> str:
        if self._consumed:
            raise GmailAuthorizationError("這次 Gmail 授權回應已使用，請重新操作。")
        self._consumed = True
        self.server.timeout = timeout
        self.server.handle_request()
        callback_path = self.server.callback_path
        if callback_path is None:
            raise GmailAuthorizationError("等待 Gmail 授權逾時，請重新操作。")
        return parse_authorization_callback(callback_path, expected_state)

    def close(self) -> None:
        self.server.server_close()


class GoogleOAuthClient:
    def __init__(
        self,
        config: OAuthClientConfig,
        *,
        http: JsonHttpClient | None = None,
        browser_opener: Callable[[str], bool] = webbrowser.open,
        receiver_factory: Callable[[], AuthorizationReceiver] = LoopbackAuthorizationReceiver,
    ) -> None:
        if not config.client_id.strip():
            raise GmailConfigurationError("此版本尚未設定 Gmail OAuth 用戶端。")
        self.config = config
        self.http = http or JsonHttpClient()
        self.browser_opener = browser_opener
        self.receiver_factory = receiver_factory

    def authorize(self) -> OAuthTokens:
        verifier = secrets.token_urlsafe(64)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        receiver = self.receiver_factory()
        try:
            url = build_authorization_url(
                self.config,
                redirect_uri=receiver.redirect_uri,
                state=state,
                code_challenge=challenge,
            )
            if self.browser_opener(url) is False:
                raise GmailAuthorizationError("無法開啟系統瀏覽器完成 Gmail 授權。")
            code = receiver.wait_for_code(state, CALLBACK_TIMEOUT_SECONDS)
        finally:
            receiver.close()

        fields = {
            "client_id": self.config.client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": receiver.redirect_uri,
        }
        if self.config.client_secret:
            fields["client_secret"] = self.config.client_secret
        try:
            response = self.http.post_form(TOKEN_ENDPOINT, fields)
        except _HttpResponseError as exc:
            raise GmailAuthorizationError("Google 無法完成 Gmail 授權，請重新操作。") from exc
        access_token = _required_string(response, "access_token")
        refresh_token = _required_string(response, "refresh_token")
        account_email = self._account_email(access_token)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            account_email=account_email,
            expires_in=_optional_positive_int(response.get("expires_in")),
        )

    def refresh(self, refresh_token: str) -> OAuthTokens:
        fields = {
            "client_id": self.config.client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if self.config.client_secret:
            fields["client_secret"] = self.config.client_secret
        try:
            response = self.http.post_form(TOKEN_ENDPOINT, fields)
        except _HttpResponseError as exc:
            if exc.error_code == "invalid_grant":
                raise GmailReauthorizationRequired("Gmail 授權已失效，需要重新授權。") from exc
            raise GmailNetworkError("Google 暫時無法更新 Gmail 授權。") from exc
        return OAuthTokens(
            access_token=_required_string(response, "access_token"),
            expires_in=_optional_positive_int(response.get("expires_in")),
        )

    def revoke(self, refresh_token: str) -> None:
        try:
            self.http.post_form_without_response(REVOKE_ENDPOINT, {"token": refresh_token})
        except _HttpResponseError as exc:
            raise GmailNetworkError("Google 端授權暫時無法撤銷。") from exc

    def _account_email(self, access_token: str) -> str:
        try:
            response = self.http.request_json(
                USER_INFO_ENDPOINT,
                method="GET",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except _HttpResponseError as exc:
            raise GmailAuthorizationError("無法確認已連結的 Gmail 帳號。") from exc
        email = _required_string(response, "email")
        if not bool(response.get("email_verified", True)):
            raise GmailAuthorizationError("Google 帳號的電子郵件地址尚未驗證。")
        return email


def build_authorization_url(
    config: OAuthClientConfig,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    query = urllib.parse.urlencode(
        {
            "access_type": "offline",
            "client_id": config.client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(OAUTH_SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZATION_ENDPOINT}?{query}"


def parse_authorization_callback(callback_path: str, expected_state: str) -> str:
    parsed = urllib.parse.urlsplit(callback_path)
    if parsed.path != "/oauth2/callback":
        raise GmailAuthorizationError("Gmail 授權回應路徑不正確。")
    try:
        values = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    except ValueError as exc:
        raise GmailAuthorizationError("Gmail 授權回應格式不正確。") from exc
    received_state = _single_query_value(values, "state")
    if not secrets.compare_digest(received_state, expected_state):
        raise GmailAuthorizationError("Gmail 授權安全驗證失敗，請重新操作。")
    if "error" in values:
        raise GmailAuthorizationError("Gmail 授權已取消或遭 Google 拒絕。")
    return _single_query_value(values, "code")


def _single_query_value(values: dict[str, list[str]], name: str) -> str:
    candidates = values.get(name, [])
    if len(candidates) != 1 or not candidates[0]:
        raise GmailAuthorizationError("Gmail 授權回應不完整。")
    return candidates[0]


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise GmailAuthorizationError("Google 回傳的 Gmail 授權內容不完整。")
    return value


def _optional_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _remote_error_code(error: urllib.error.HTTPError) -> str | None:
    try:
        raw = error.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        code = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(code, dict):
            code = code.get("status")
        return str(code) if code else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _oauth_config_paths() -> tuple[Path, ...]:
    packaged_root = Path(getattr(sys, "_MEIPASS", Path.cwd()))
    project_root = Path(__file__).resolve().parents[4]
    return (
        packaged_root / "resources" / "google_oauth_client.json",
        project_root / "resources" / "google_oauth_client.json",
    )
