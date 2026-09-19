"""Boundaries and secret-safe value objects for Gmail integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class GmailInfrastructureError(RuntimeError):
    """Base error translated by the application service."""


class GmailConfigurationError(GmailInfrastructureError):
    """Raised when the publisher OAuth client is unavailable."""


class GmailNetworkError(GmailInfrastructureError):
    """Raised for temporary network and remote-service failures."""


class GmailAuthorizationError(GmailInfrastructureError):
    """Raised when interactive authorization cannot complete safely."""


class GmailReauthorizationRequired(GmailAuthorizationError):
    """Raised when a stored refresh token is no longer accepted."""


class GmailMessageError(GmailInfrastructureError):
    """Raised when an outgoing message is invalid or too large."""


class CredentialStoreError(GmailInfrastructureError):
    """Raised when secure credential storage is unavailable."""


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    account_email: str
    refresh_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    account_email: str | None = None
    expires_in: int | None = None


class CredentialStore(Protocol):
    def read(self) -> CredentialRecord | None: ...

    def write(self, record: CredentialRecord) -> None: ...

    def delete(self) -> None: ...


class OAuthGateway(Protocol):
    def authorize(self) -> OAuthTokens: ...

    def refresh(self, refresh_token: str) -> OAuthTokens: ...

    def revoke(self, refresh_token: str) -> None: ...


class GmailGateway(Protocol):
    def send(
        self,
        *,
        access_token: str,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        attachment: Path,
    ) -> str: ...
