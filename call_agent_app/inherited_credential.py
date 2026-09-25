"""Inherit a fresh Agents Platform credential from the Agents Platform
Runners app — over the workspace's own public API, not a file read.

``settings.py``'s step 3 (a read of
``$AW_WORKSPACE_HOME/app-config/agents-platform-runners.json``) is correct
for standalone mode but unreachable inside this app's Tier-2 container: the
container mounts no app-config volume, and even the path computation
resolves to a directory absent from the image (see the manifest vocabulary
in ``src/apps/runtime.py`` and the design card
``bug:call-agent-agents-platform-token-rejected``). The runners app's token
IS kept fresh, though, and is reachable from any container over
``GET {AW_WORKSPACE_API_URL}/api/apps/agents-platform-runners/config`` with
``X-Api-Key`` — the same public tunnel URL and shared workspace secret
``apps/signoz`` and ``apps/mcp-gateway`` already carry into a Tier-2
container.

This polls that endpoint on a timer and caches the result in memory only —
never written back into this app's own config, which would manufacture the
exact stale local copy this module exists to avoid. ``token()``/``base()``
are synchronous cache reads: ``settings.resolve()`` is called per request
from live voice-call paths (``routes.py``, ``__main__.py``'s realtime/duplex
factories) and must never block the event loop on an HTTP fetch. All I/O
lives here, in the poll loop and in ``refresh_now()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

log = logging.getLogger("aw_apps.call_agent.inherited_credential")

RUNNERS_APP_ID = "agents-platform-runners"

#: The upstream token is 24h and the runners app refreshes at half-life
#: (6h) — 5 minutes is far inside that margin, same rationale as
#: ``wan_watch.WanAddressWatch.DEFAULT_INTERVAL``.
DEFAULT_INTERVAL = 300.0

API_URL_ENV = "AW_WORKSPACE_API_URL"
API_KEY_ENV = "AW_WORKSPACE_API_KEY"


class InheritedCredential:
    """Poll the Agents Platform Runners app's own config for a fresh
    ``agents_platform_token`` / ``agents_platform_base`` and cache it."""

    def __init__(self, interval: float = DEFAULT_INTERVAL):
        self.interval = interval
        self._token = ""
        self._base = ""
        self.last_checked: float | None = None
        self.last_error: str = ""
        self._warned_unconfigured = False
        self._task: asyncio.Task | None = None

    # ── what settings.resolve() and the doctor route read ──────────────────
    def token(self) -> str:
        """Sync, non-blocking, cache read only — never performs I/O."""
        return self._token

    def base(self) -> str:
        """Sync, non-blocking, cache read only — never performs I/O."""
        return self._base

    def configured(self) -> bool:
        return bool(os.environ.get(API_URL_ENV) and os.environ.get(API_KEY_ENV))

    def status(self) -> dict:
        return {
            "configured": self.configured(),
            "has_token": bool(self._token),
            "last_checked": self.last_checked,
            "error": self.last_error,
        }

    # ── one pass, also the unit of testing ─────────────────────────────────
    async def refresh_now(self) -> None:
        api_url = os.environ.get(API_URL_ENV, "").strip()
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_url or not api_key:
            # Inert-and-silent is the exact failure mode this card is about —
            # say so once, loudly, not on every 300s tick.
            if not self._warned_unconfigured:
                log.warning(
                    "%s / %s not set — cannot inherit a credential from the "
                    "%s app; this app comes up inert on this path until "
                    "both are present in the container's environment.",
                    API_URL_ENV, API_KEY_ENV, RUNNERS_APP_ID)
                self._warned_unconfigured = True
            self.last_error = f"{API_URL_ENV} / {API_KEY_ENV} not set"
            self.last_checked = time.time()
            return

        url = f"{api_url.rstrip('/')}/api/apps/{RUNNERS_APP_ID}/config"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"X-Api-Key": api_key})
                resp.raise_for_status()
                body = resp.json()
            # Core snapshots either the bare config dict or wraps it under
            # "config" depending on version — accept both, same rule
            # settings.py's file-based read already uses.
            cfg = (body.get("config") if isinstance(body, dict)
                   and isinstance(body.get("config"), dict) else body)
            cfg = cfg if isinstance(cfg, dict) else {}
            token = str(cfg.get("agents_platform_token") or "").strip()
            base = str(cfg.get("agents_platform_base") or "").strip()
        except Exception as exc:  # noqa: BLE001 — a fetch hiccup keeps the last good value
            self.last_error = str(exc)
            log.warning("could not fetch %s's config: %s", RUNNERS_APP_ID, exc)
            self.last_checked = time.time()
            return

        if token:
            self._token = token
        if base:
            self._base = base
        self.last_error = ""
        self.last_checked = time.time()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_now()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a watcher that dies is worse than one that errs
                log.exception("inherited-credential refresh failed")
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
