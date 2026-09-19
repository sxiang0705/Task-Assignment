"""Gmail OAuth, credential storage, and message transport infrastructure."""

from task_assignment.infrastructure.gmail.client import (
    MAX_ATTACHMENT_BYTES,
    GmailApiClient,
)
from task_assignment.infrastructure.gmail.contracts import (
    CredentialRecord,
    CredentialStore,
    CredentialStoreError,
    GmailAuthorizationError,
    GmailConfigurationError,
    GmailGateway,
    GmailInfrastructureError,
    GmailMessageError,
    GmailNetworkError,
    GmailReauthorizationRequired,
    OAuthGateway,
    OAuthTokens,
)
from task_assignment.infrastructure.gmail.credential_store import (
    MemoryCredentialStore,
    WindowsCredentialStore,
)
from task_assignment.infrastructure.gmail.oauth import (
    EMAIL_SCOPE,
    GMAIL_SEND_SCOPE,
    OAUTH_SCOPES,
    GoogleOAuthClient,
    LoopbackAuthorizationReceiver,
    OAuthClientConfig,
    build_authorization_url,
    parse_authorization_callback,
)

__all__ = [
    "EMAIL_SCOPE",
    "GMAIL_SEND_SCOPE",
    "MAX_ATTACHMENT_BYTES",
    "OAUTH_SCOPES",
    "CredentialRecord",
    "CredentialStore",
    "CredentialStoreError",
    "GmailApiClient",
    "GmailAuthorizationError",
    "GmailConfigurationError",
    "GmailGateway",
    "GmailInfrastructureError",
    "GmailMessageError",
    "GmailNetworkError",
    "GmailReauthorizationRequired",
    "GoogleOAuthClient",
    "LoopbackAuthorizationReceiver",
    "MemoryCredentialStore",
    "OAuthClientConfig",
    "OAuthGateway",
    "OAuthTokens",
    "WindowsCredentialStore",
    "build_authorization_url",
    "parse_authorization_callback",
]
