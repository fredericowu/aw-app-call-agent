"""Unit tests for the HTTP-based credential inheritance
(``inherited_credential.InheritedCredential``) and its precedence in
``settings.resolve()`` — step 4, added after the file-based step 3.

Deliberately split from ``test_routes.py``'s 401-retry tests: those prove the
OTHER half of the design (refresh-on-401-and-retry-once). Mutating either
half independently should break exactly one of these two files, not both —
see the design card's "mutation-test the two halves separately" instruction.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from call_agent_app import settings as settings_mod  # noqa: E402
from call_agent_app.inherited_credential import InheritedCredential  # noqa: E402


class _FakeClient:
    """``httpx.AsyncClient`` stand-in — one canned response or exception."""

    def __init__(self, response=None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def __call__(self, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, headers=None):
        self.calls.append({"url": url, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._body


def test_refresh_now_fetches_and_caches_the_token(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://api.test.workspace")
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "wskey")
    response = _FakeResponse(200, {"config": {
        "agents_platform_token": "fresh-tok",
        "agents_platform_base": "http://ap.internal",
    }})
    fake = _FakeClient(response=response)
    monkeypatch.setattr(httpx, "AsyncClient", fake)

    cred = InheritedCredential()
    asyncio.run(cred.refresh_now())

    assert cred.token() == "fresh-tok"
    assert cred.base() == "http://ap.internal"
    assert cred.last_error == ""
    assert cred.last_checked is not None
    assert fake.calls[0]["url"] == (
        "https://api.test.workspace/api/apps/agents-platform-runners/config")
    assert fake.calls[0]["headers"]["X-Api-Key"] == "wskey"


def test_bare_config_dict_without_a_wrapper_is_also_accepted(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://api.test.workspace")
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "wskey")
    response = _FakeResponse(200, {"agents_platform_token": "bare-tok"})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(response=response))

    cred = InheritedCredential()
    asyncio.run(cred.refresh_now())
    assert cred.token() == "bare-tok"


def test_missing_env_vars_leaves_it_inert(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_API_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_API_KEY", raising=False)

    cred = InheritedCredential()
    assert cred.configured() is False
    asyncio.run(cred.refresh_now())

    assert cred.token() == ""
    assert cred.status()["configured"] is False
    assert "not set" in cred.last_error


def test_fetch_failure_keeps_the_last_good_value(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_API_URL", "https://api.test.workspace")
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "wskey")
    cred = InheritedCredential()

    good = _FakeResponse(200, {"agents_platform_token": "good-tok"})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(response=good))
    asyncio.run(cred.refresh_now())
    assert cred.token() == "good-tok"

    monkeypatch.setattr(httpx, "AsyncClient",
                        _FakeClient(exc=httpx.ConnectError("boom")))
    asyncio.run(cred.refresh_now())
    assert cred.token() == "good-tok"        # unchanged
    assert cred.last_error                   # but the failure is recorded
    assert cred.status()["error"]


def test_settings_resolve_own_config_wins_over_inherited():
    cred = InheritedCredential()
    cred._token = "inherited-tok"
    cred._base = "http://inherited"

    s = settings_mod.resolve(
        {"agents_platform_base": "http://own", "agents_platform_token": "own-tok"},
        inherited=cred)

    assert s.agents_platform_base == "http://own"
    assert s.agents_platform_token == "own-tok"
    assert s.source == "app-config"


def test_settings_resolve_falls_back_to_the_inherited_credential(monkeypatch):
    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_TOKEN", raising=False)
    monkeypatch.setattr(settings_mod, "_runners_config", dict)  # step 3: nothing

    cred = InheritedCredential()
    cred._token = "api-tok"
    cred._base = "http://api-base"

    s = settings_mod.resolve({}, inherited=cred)

    assert s.agents_platform_base == "http://api-base"
    assert s.agents_platform_token == "api-tok"
    assert s.source == "inherited:agents-platform-runners@api"


def test_settings_resolve_with_no_inherited_object_skips_step_4(monkeypatch):
    monkeypatch.delenv("AW_AGENTS_PLATFORM_BASE", raising=False)
    monkeypatch.delenv("AW_AGENTS_PLATFORM_TOKEN", raising=False)
    monkeypatch.setattr(settings_mod, "_runners_config", dict)

    s = settings_mod.resolve({})  # inherited=None, the default

    assert s.agents_platform_base == ""
    assert s.agents_platform_token == ""
    assert s.source == "unset"
