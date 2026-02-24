from __future__ import annotations

"""
Daemon/Bounce (simplificado)

- NO toca Notion.
- /bounce: recibe una URL, la relanza y devuelve el body (raw) por defecto.
  - debug=1 -> devuelve JSON con detalles
- /tick: llama al Bridge "inbox endpoint A" (BRIDGE_INBOX_URL) para que procese Notion.
  - Devuelve resultado compacto (raw) por defecto, JSON si debug=1.

Env:
- DAEMON_TOKEN (opcional): si está, exige token=... o header X-DAEMON-TOKEN
- BRIDGE_INBOX_URL (recomendado): URL completa para llamar al bridge, puede incluir token del bridge
  ej: https://claude-bridge.../notion/inbox_tick?token=XYZ
- BOUNCE_TIMEOUT_SEC (default 20)
- BOUNCE_MAX_HOPS (default 1)
- TICK_TIMEOUT_SEC (default 25)
"""

import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlencode

from flask import Blueprint, Response, jsonify, request

from .bounce import forward_get
from .state import load_state, now_ts, save_state

bp = Blueprint("daemon_bounce", __name__)


def _env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name)
    if v is None:
        return "" if default is None else str(default)
    return str(v)


def _bool_q(name: str, default: bool = False) -> bool:
    v = request.args.get(name)
    if v is None:
        return default
    return v in ("1", "true", "True", "yes", "on")


def _int_q(name: str, default: int, min_v: Optional[int] = None, max_v: Optional[int] = None) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        n = default
    else:
        try:
            n = int(raw)
        except Exception:
            raise ValueError(f"{name} inválido")
    if min_v is not None and n < min_v:
        raise ValueError(f"{name} debe ser >= {min_v}")
    if max_v is not None and n > max_v:
        raise ValueError(f"{name} debe ser <= {max_v}")
    return n


def _float_q(name: str, default: float, min_v: Optional[float] = None, max_v: Optional[float] = None) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        x = default
    else:
        try:
            x = float(raw)
        except Exception:
            raise ValueError(f"{name} inválido")
    if min_v is not None and x < min_v:
        raise ValueError(f"{name} debe ser >= {min_v}")
    if max_v is not None and x > max_v:
        raise ValueError(f"{name} debe ser <= {max_v}")
    return x


def _json_error(msg: str, status: int = 400, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    return jsonify(payload), status


def _require_token_if_configured():
    expected = (_env("DAEMON_TOKEN", "") or "").strip()
    if not expected:
        return None
    provided = (request.headers.get("X-DAEMON-TOKEN") or request.args.get("token") or "").strip()
    if provided != expected:
        return _json_error("Unauthorized", 401)
    return None


def _state_path() -> str:
    return _env("STATE_FILE", "/tmp/daemon_bounce_state.json")


def _load_state() -> Dict[str, Any]:
    return load_state(_state_path())


def _save_state(state: Dict[str, Any]) -> None:
    save_state(_state_path(), state)


def _bridge_inbox_url() -> str:
    u = (_env("BRIDGE_INBOX_URL", "") or "").strip()
    if not u:
        raise RuntimeError("Falta BRIDGE_INBOX_URL (ej: https://claude-bridge.../notion/inbox_tick?token=XYZ)")
    return u


@bp.before_request
def _auth_gate():
    if request.path.endswith("/health"):
        return None
    return _require_token_if_configured()


@bp.get("/health")
def health():
    return jsonify(
        ok=True,
        service="daemon-bounce",
        version="2.0.0",
        now=now_ts(),
        state_file=_state_path(),
        has_bridge_inbox_url=bool((_env("BRIDGE_INBOX_URL", "") or "").strip()),
    )


@bp.get("/state")
def state_endpoint():
    return jsonify({"ok": True, "state": _load_state()})


@bp.get("/bounce")
def bounce_endpoint():
    url = (request.args.get("url") or "").strip()
    debug = _bool_q("debug", False)
    if not url:
        return _json_error("Falta url", 400)

    try:
        timeout_sec = _float_q("timeout", float(_env("BOUNCE_TIMEOUT_SEC", "20")), min_v=0.5, max_v=120.0)
        max_hops = _int_q("max_hops", int(_env("BOUNCE_MAX_HOPS", "1")), min_v=1, max_v=10)
    except ValueError as e:
        return _json_error(str(e), 400)

    try:
        res = forward_get(url, timeout_sec=timeout_sec, max_hops=max_hops)
    except Exception as e:
        return _json_error(f"Bounce error: {e}", 502, url=url)

    # Por defecto: raw body para que Bridge pueda volcarlo tal cual en Notion
    if not debug:
        body = res.get("body_text") or ""
        return Response(body, status=res.get("status_code") or 200, content_type="text/plain; charset=utf-8")

    return jsonify(ok=True, action="bounce", result=res)


@bp.get("/tick")
def tick():
    """
    Tick simple: pide al Bridge que procese la inbox de Notion.
    """
    debug = _bool_q("debug", False)
    force = _bool_q("force", False)

    try:
        timeout_sec = _float_q("timeout", float(_env("TICK_TIMEOUT_SEC", "25")), min_v=0.5, max_v=180.0)
    except ValueError as e:
        return _json_error(str(e), 400)

    state = _load_state()
    state["last_check"] = now_ts()

    bridge_url = _bridge_inbox_url()
    if force:
        # añade force=1 si el endpoint del bridge lo usa (no es obligatorio)
        sep = "&" if "?" in bridge_url else "?"
        bridge_url = f"{bridge_url}{sep}force=1"

    t0 = time.perf_counter()
    try:
        res = forward_get(bridge_url, timeout_sec=timeout_sec, max_hops=1)
    except Exception as e:
        state["last_error"] = str(e)
        _save_state(state)
        return _json_error(f"Tick error: {e}", 502)

    latency_ms = int((time.perf_counter() - t0) * 1000)
    state["last_latency_ms"] = latency_ms
    state["last_status_code"] = res.get("status_code")
    state["last_error"] = None
    _save_state(state)

    if not debug:
        # raw body tal cual
        body = res.get("body_text") or ""
        return Response(body, status=res.get("status_code") or 200, content_type="text/plain; charset=utf-8")

    return jsonify(ok=True, action="tick", latency_ms=latency_ms, bridge_url=bridge_url, result=res)
