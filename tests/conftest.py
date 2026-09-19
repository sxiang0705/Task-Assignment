from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolate_publisher_credentials(monkeypatch):
    """In-process tests must not discover the publisher's real OAuth or vault."""
    from task_assignment.application import services
    from task_assignment.infrastructure.gmail import MemoryCredentialStore, OAuthClientConfig

    monkeypatch.setattr(OAuthClientConfig, "load", classmethod(lambda cls: None))
    monkeypatch.setattr(services, "WindowsCredentialStore", MemoryCredentialStore)
