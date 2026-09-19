from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from task_assignment.application import ServiceConflictError, ServiceValidationError, TaskDraft
from task_assignment.application.services import create_services
from task_assignment.config import AppPaths
from task_assignment.domain.enums import ScheduleMode
from task_assignment.infrastructure.database import Database
from task_assignment.infrastructure.gmail import (
    CredentialRecord,
    GmailNetworkError,
    GmailReauthorizationRequired,
    MemoryCredentialStore,
    OAuthTokens,
)

NOW = datetime(2026, 2, 3, 14, 5, 6)
ACCESS_TOKEN = "temporary-access-token-value"
REFRESH_TOKEN = "persistent-refresh-token-value"


class FakeOAuth:
    def __init__(self) -> None:
        self.authorize_calls = 0
        self.refresh_calls = 0
        self.revoked: list[str] = []
        self.refresh_error: Exception | None = None
        self.revoke_error: Exception | None = None

    def authorize(self) -> OAuthTokens:
        self.authorize_calls += 1
        return OAuthTokens(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            account_email="owner@example.com",
            expires_in=3600,
        )

    def refresh(self, refresh_token: str) -> OAuthTokens:
        self.refresh_calls += 1
        assert refresh_token == REFRESH_TOKEN
        if self.refresh_error is not None:
            raise self.refresh_error
        return OAuthTokens(access_token=ACCESS_TOKEN, expires_in=3600)

    def revoke(self, refresh_token: str) -> None:
        if self.revoke_error is not None:
            raise self.revoke_error
        self.revoked.append(refresh_token)


class FakeGmailGateway:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.error: Exception | None = None

    def send(self, **values: object) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(values)
        return "message-123"


def _context(
    root: Path,
    *,
    credentials: MemoryCredentialStore | None = None,
    oauth: FakeOAuth | None = None,
    gmail: FakeGmailGateway | None = None,
):
    paths = AppPaths.from_base_dir(root)
    paths.ensure_directories()
    database = Database(paths.database)
    database.initialize()
    credential_store = credentials or MemoryCredentialStore()
    oauth_gateway = oauth or FakeOAuth()
    gmail_gateway = gmail or FakeGmailGateway()
    services = create_services(
        database,
        paths=paths,
        now_provider=lambda: NOW,
        credential_store=credential_store,
        oauth_gateway=oauth_gateway,
        gmail_gateway=gmail_gateway,
    )
    return paths, database, services, credential_store, oauth_gateway, gmail_gateway


def _add_due_items(services) -> None:
    services.tasks.create(
        TaskDraft(
            name="複習 OAuth",
            description="確認今日與逾期摘要",
            start_at=datetime(2026, 2, 1, 9),
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(
                datetime(2026, 2, 2, 9),
                datetime(2026, 2, 3, 9),
            ),
        )
    )


def test_connect_persists_refresh_token_across_service_recreation(tmp_path: Path) -> None:
    credentials = MemoryCredentialStore()
    oauth = FakeOAuth()
    paths, _, services, _, _, _ = _context(tmp_path / "app", credentials=credentials, oauth=oauth)

    status = services.gmail.connect()

    assert status.linked is True
    assert status.account_email == "owner@example.com"
    assert credentials.record == CredentialRecord("owner@example.com", REFRESH_TOKEN)
    _, _, recreated, _, _, _ = _context(paths.base_dir, credentials=credentials, oauth=oauth)
    assert recreated.gmail.status().linked is True
    assert oauth.authorize_calls == 1


def test_prepare_and_send_uses_saved_recipient_summary_refresh_and_logs(tmp_path: Path) -> None:
    credentials = MemoryCredentialStore(CredentialRecord("owner@example.com", REFRESH_TOKEN))
    paths, database, services, _, oauth, gmail = _context(tmp_path / "app", credentials=credentials)
    _add_due_items(services)
    services.gmail.set_default_recipient("recipient@example.net")

    draft = services.gmail.prepare_email()
    result = services.gmail.send_prepared(
        draft,
        recipient=draft.recipient,
        subject=draft.subject,
        body=draft.body,
    )

    assert draft.recipient == "recipient@example.net"
    assert "今日待複習（1）" in draft.body
    assert "逾期未處理（1）" in draft.body
    assert "複習 OAuth" in draft.body
    assert draft.backup_path.is_file()
    assert result.message_id == "message-123"
    assert result.backup_path.is_file()
    assert oauth.refresh_calls == 1
    assert oauth.authorize_calls == 0
    assert gmail.sent[0]["access_token"] == ACCESS_TOKEN
    assert gmail.sent[0]["attachment"] == draft.backup_path
    assert list(paths.backups.glob("task-assignment-backup-*.zip"))
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT recipient, success, gmail_message_id, backup_filename
            FROM backup_logs WHERE operation = 'gmail_send'
            """
        ).fetchone()
    assert tuple(row) == (
        "recipient@example.net",
        1,
        "message-123",
        draft.backup_filename,
    )


def test_invalid_refresh_token_reauthorizes_only_during_manual_send(tmp_path: Path) -> None:
    credentials = MemoryCredentialStore(CredentialRecord("owner@example.com", REFRESH_TOKEN))
    oauth = FakeOAuth()
    oauth.refresh_error = GmailReauthorizationRequired("需要重新授權。")
    _, _, services, _, _, gmail = _context(tmp_path / "app", credentials=credentials, oauth=oauth)

    assert services.gmail.status().linked is True
    assert oauth.refresh_calls == 0
    assert oauth.authorize_calls == 0
    draft = services.gmail.prepare_email()
    services.gmail.send_prepared(
        draft,
        recipient="recipient@example.net",
        subject=draft.subject,
        body=draft.body,
    )

    assert oauth.refresh_calls == 1
    assert oauth.authorize_calls == 1
    assert len(gmail.sent) == 1
    assert credentials.record == CredentialRecord("owner@example.com", REFRESH_TOKEN)


def test_send_failure_keeps_zip_logs_safe_error_and_never_persists_tokens(
    tmp_path: Path,
) -> None:
    credentials = MemoryCredentialStore(CredentialRecord("owner@example.com", REFRESH_TOKEN))
    gmail = FakeGmailGateway()
    gmail.error = GmailNetworkError("Gmail 暫時無法使用。")
    _, database, services, _, _, _ = _context(
        tmp_path / "app", credentials=credentials, gmail=gmail
    )
    draft = services.gmail.prepare_email()

    with pytest.raises(ServiceConflictError, match="暫時無法使用"):
        services.gmail.send_prepared(
            draft,
            recipient="recipient@example.net",
            subject=draft.subject,
            body=draft.body,
        )

    assert draft.backup_path.is_file()
    with database.connection() as connection:
        row = connection.execute(
            "SELECT success, error_message FROM backup_logs WHERE operation = 'gmail_send'"
        ).fetchone()
        settings_payload = " ".join(
            str(item[0]) for item in connection.execute("SELECT value_json FROM settings")
        )
    assert tuple(row) == (0, "Gmail 暫時無法使用。")
    assert ACCESS_TOKEN not in settings_payload
    assert REFRESH_TOKEN not in settings_payload
    assert ACCESS_TOKEN.encode() not in database.path.read_bytes()
    assert REFRESH_TOKEN.encode() not in database.path.read_bytes()


def test_disconnect_revokes_when_possible_and_always_removes_local_token(
    tmp_path: Path,
) -> None:
    credentials = MemoryCredentialStore(CredentialRecord("owner@example.com", REFRESH_TOKEN))
    oauth = FakeOAuth()
    _, _, services, _, _, _ = _context(tmp_path / "app", credentials=credentials, oauth=oauth)

    result = services.gmail.disconnect()

    assert result.status.linked is False
    assert result.warning is None
    assert credentials.record is None
    assert oauth.revoked == [REFRESH_TOKEN]

    draft = services.gmail.prepare_email()
    services.gmail.send_prepared(
        draft,
        recipient="recipient@example.net",
        subject=draft.subject,
        body=draft.body,
    )
    assert oauth.authorize_calls == 1


def test_disconnect_removes_local_token_when_remote_revoke_fails(tmp_path: Path) -> None:
    credentials = MemoryCredentialStore(CredentialRecord("owner@example.com", REFRESH_TOKEN))
    oauth = FakeOAuth()
    oauth.revoke_error = GmailNetworkError("offline")
    _, _, services, _, _, _ = _context(tmp_path / "app", credentials=credentials, oauth=oauth)

    result = services.gmail.disconnect()

    assert credentials.record is None
    assert result.status.linked is False
    assert result.warning is not None
    assert "Google 端授權" in result.warning


def test_connect_rejects_incomplete_tokens_without_persisting_them(tmp_path: Path) -> None:
    credentials = MemoryCredentialStore()
    oauth = FakeOAuth()
    oauth.authorize = lambda: OAuthTokens(
        access_token="",
        refresh_token=REFRESH_TOKEN,
        account_email="owner@example.com",
    )
    _, _, services, _, _, _ = _context(tmp_path / "app", credentials=credentials, oauth=oauth)

    with pytest.raises(ServiceConflictError, match="access token"):
        services.gmail.connect()

    assert credentials.record is None


@pytest.mark.parametrize(
    "recipient",
    ("", "not-an-email", "two@example.com,other@example.com", "bad\n@example.com"),
)
def test_invalid_recipient_is_rejected_before_backup_or_oauth(
    tmp_path: Path, recipient: str
) -> None:
    paths, _, services, _, oauth, gmail = _context(tmp_path / "app")

    with pytest.raises(ServiceValidationError):
        services.gmail.set_default_recipient(recipient or "not-an-email")

    assert not list(paths.backups.glob("*.zip"))
    assert oauth.authorize_calls == 0
    assert not gmail.sent
