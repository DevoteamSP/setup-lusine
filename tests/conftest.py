"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.fake_github import FakeGitHub, serve

REPO = "DevoteamSP/acme"
REPO_URL = f"https://github.com/{REPO}"


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    """A fake GitHub the script talks to."""
    state = FakeGitHub(repos={REPO: {"README.md": b"# Acme\n", "doc/guide.md": b"guide v1\n"}})
    server, url = serve(state)
    yield state
    server.shutdown()
    server.server_close()
