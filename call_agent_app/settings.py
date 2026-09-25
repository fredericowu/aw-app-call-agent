"""Where a call's settings come from.

This app needs two things it does not own: the agents-platform base URL and
an identity JWT for it. Both already exist in a workspace that runs agents —
the **Agents Platform Runners** app holds them, mints nothing new, and is
installed on every workspace where a call could possibly work. Asking the
user to paste a JWT into a second app just to make the first call is a
setup step with no decision in it, so the resolution order is:

1. this app's own config (``agents_platform_base`` / ``agents_platform_token``)
   — set these to point calls at a *different* platform than the runners app,
   or as a break-glass override. A value here wins and is never refreshed;
2. environment (``AW_AGENTS_PLATFORM_BASE`` / ``AW_AGENTS_PLATFORM_TOKEN``) —
   how standalone mode is configured, since there is no workspace config there;
3. the runners app's saved config snapshot on disk, at
   ``$AW_WORKSPACE_HOME/app-config/agents-platform-runners.json``. Correct
   for standalone mode running directly on the workspace host; **unreachable
   in this app's own Tier-2 container**, which mounts no app-config volume
   and cannot even compute the right path there (no ``AW_WORKSPACE*`` var
   reaches the container). Kept for the standalone case, not removed;
4. ``inherited_credential.InheritedCredential``'s in-memory cache — the same
   runners-app token, kept fresh by a background poll of
   ``GET {AW_WORKSPACE_API_URL}/api/apps/agents-platform-runners/config``
   over the workspace's own public API. This is what actually reaches the
   Tier-2 container: see ``inherited_credential.py`` for why and how.

Step 3 is a read of another app's file, which the capability model has no
verb for — there is no ``config:read:<app>`` today, only
``config:extend:<app>`` for writing; step 4 sidesteps that by asking the
workspace's own API instead. Both are best-effort and read-only: a missing
file or a failed fetch just leaves the field blank/stale, and
``GET /settings`` reports which source won (``inherited:agents-platform-
runners@api`` for step 4) so a blank one is diagnosable. If a
``config:read:<app>`` capability lands later, step 3 could be simplified to
use it — step 4 stays either way, since a Tier-2 container needs the HTTP
path regardless.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .service import CallSettings

log = logging.getLogger("aw_apps.call_agent.settings")

RUNNERS_APP_ID = "agents-platform-runners"


def _workspace_home() -> Path:
    return Path(os.environ.get("AW_WORKSPACE_HOME")
                or os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
                + "/.aw-workspace")


def _runners_config() -> dict:
    """Best-effort read of the Agents Platform Runners app's saved config."""
    path = _workspace_home() / "app-config" / f"{RUNNERS_APP_ID}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    # Core snapshots either the bare config dict or wraps it under "config"
    # depending on version — accept both rather than guessing wrong silently.
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    return cfg if isinstance(cfg, dict) else {}


def _num(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def resolve(config: dict | None, inherited=None) -> CallSettings:
    """Build the effective ``CallSettings`` for this workspace.

    ``inherited`` is an ``inherited_credential.InheritedCredential`` (or
    anything with the same ``token()``/``base()`` cache-read methods) — kept
    as a parameter rather than imported and constructed here so this
    function stays pure and synchronous: it never performs I/O itself, only
    reads whatever cache the caller already owns. Callers on the container
    path (``__main__.py``) pass the live poller; a caller with none (tests,
    standalone-without-a-poller) gets step 4 skipped, same as a step 3 file
    miss.
    """
    cfg = config or {}
    defaults = CallSettings()

    base = (cfg.get("agents_platform_base") or "").strip()
    token = (cfg.get("agents_platform_token") or "").strip()
    source = "app-config"

    if not base or not token:
        env_base = (os.environ.get("AW_AGENTS_PLATFORM_BASE") or "").strip()
        env_token = (os.environ.get("AW_AGENTS_PLATFORM_TOKEN") or "").strip()
        if env_base or env_token:
            base = base or env_base
            token = token or env_token
            source = "env"

    if not base or not token:
        runners = _runners_config()
        file_base = (runners.get("agents_platform_base") or "").strip()
        file_token = (runners.get("agents_platform_token") or "").strip()
        if file_base or file_token:
            base = base or file_base
            token = token or file_token
            source = f"inherited:{RUNNERS_APP_ID}"

    if (not base or not token) and inherited is not None:
        api_base = (inherited.base() or "").strip()
        api_token = (inherited.token() or "").strip()
        if api_base or api_token:
            base = base or api_base
            token = token or api_token
            source = f"inherited:{RUNNERS_APP_ID}@api"

    if not base and not token:
        source = "unset"

    return CallSettings(
        agents_platform_base=base,
        agents_platform_token=token,
        agent_slug=(cfg.get("agent_slug") or defaults.agent_slug).strip(),
        external_id=(cfg.get("external_id") or defaults.external_id).strip(),
        prompt_template=cfg.get("prompt_template") or defaults.prompt_template,
        default_voice_lang=(cfg.get("default_voice_lang")
                            or defaults.default_voice_lang).strip(),
        stt_provider=(cfg.get("stt_provider") or defaults.stt_provider).strip(),
        stt_openai_model=(cfg.get("stt_openai_model")
                          or defaults.stt_openai_model).strip(),
        stt_realtime_model=(cfg.get("stt_realtime_model")
                            or defaults.stt_realtime_model).strip(),
        stt_realtime_delay=(cfg.get("stt_realtime_delay")
                            or defaults.stt_realtime_delay).strip(),
        tts_provider=(cfg.get("tts_provider") or defaults.tts_provider).strip(),
        tts_openai_model=(cfg.get("tts_openai_model")
                          or defaults.tts_openai_model).strip(),
        tts_openai_voice=(cfg.get("tts_openai_voice")
                          or defaults.tts_openai_voice).strip(),
        openai_api_key=(cfg.get("openai_api_key") or "").strip(),
        voice_runtime=(cfg.get("voice_runtime") or defaults.voice_runtime).strip(),
        realtime_model=(cfg.get("realtime_model") or defaults.realtime_model).strip(),
        realtime_voice=(cfg.get("realtime_voice") or defaults.realtime_voice).strip(),
        speech_pause_ms=_num(cfg.get("speech_pause_ms"), defaults.speech_pause_ms),
        poll_interval_seconds=_num(cfg.get("poll_interval_seconds"),
                                   defaults.poll_interval_seconds),
        max_poll_seconds=_num(cfg.get("max_poll_seconds"), defaults.max_poll_seconds),
        source=source,
    )
