"""MIME construction and Gmail messages.send transport."""

from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

from task_assignment.infrastructure.gmail.contracts import (
    GmailAuthorizationError,
    GmailMessageError,
    GmailNetworkError,
)
from task_assignment.infrastructure.gmail.oauth import JsonHttpClient, _HttpResponseError

GMAIL_SEND_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024
MAX_SUBJECT_LENGTH = 180
MAX_BODY_LENGTH = 100_000


class GmailApiClient:
    def __init__(self, http: JsonHttpClient | None = None) -> None:
        self.http = http or JsonHttpClient()

    def send(
        self,
        *,
        access_token: str,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        attachment: Path,
    ) -> str:
        if not access_token:
            raise GmailAuthorizationError("缺少可用的 Gmail access token。")
        if not attachment.is_file() or attachment.suffix.lower() != ".zip":
            raise GmailMessageError("找不到要寄送的 ZIP 備份。")
        try:
            attachment_size = attachment.stat().st_size
        except OSError as exc:
            raise GmailMessageError("無法讀取要寄送的 ZIP 備份。") from exc
        if attachment_size > MAX_ATTACHMENT_BYTES:
            raise GmailMessageError("備份 ZIP 超過 24 MiB，無法作為 Gmail 附件寄送。")
        if not subject.strip() or len(subject) > MAX_SUBJECT_LENGTH:
            raise GmailMessageError("郵件主旨不可空白，且不得超過 180 個字元。")
        if len(body) > MAX_BODY_LENGTH:
            raise GmailMessageError("郵件本文過長，請縮短後再寄送。")

        message = EmailMessage()
        try:
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = subject.strip()
        except ValueError as exc:
            raise GmailMessageError("寄件人、收件人或主旨格式不正確。") from exc
        message.set_content(body)
        try:
            payload = attachment.read_bytes()
        except OSError as exc:
            raise GmailMessageError("無法讀取要寄送的 ZIP 備份。") from exc
        message.add_attachment(
            payload,
            maintype="application",
            subtype="zip",
            filename=attachment.name,
        )
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        request_payload = json.dumps({"raw": encoded}).encode("utf-8")
        try:
            response = self.http.request_json(
                GMAIL_SEND_ENDPOINT,
                method="POST",
                data=request_payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                timeout=60,
            )
        except _HttpResponseError as exc:
            if exc.status in {401, 403}:
                raise GmailAuthorizationError("Gmail 拒絕目前授權，請重新授權後再試。") from exc
            if exc.status == 413:
                raise GmailMessageError("Gmail 拒絕附件大小，備份 ZIP 已保留在本機。") from exc
            raise GmailNetworkError("Gmail API 無法寄送郵件，請稍後再試。") from exc
        message_id = response.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise GmailNetworkError("Gmail 未回傳有效的郵件識別碼。")
        return message_id
