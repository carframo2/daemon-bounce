from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, request

from .bounce import forward_get, sha256_text
from .notion import extract_urls_from_page, normalize_page_id, search_page_by_title
from .state import load_state, now_ts, save_state

bp = Blueprint('daemon_bounce', __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name)
    if v is None:
        return '' if default is None else str(default)
    return str(v)


def _bool_q(name: str, default: bool = False) -> bool:
    v = request.args.get(name)
    if v is None:
        return default
    return v in ('1', 'true', 'True', 'yes', 'on')


def _int_q(name: str, default: int, min_v: Optional[int] = None, max_v: Optional[int] = None) -> int:
    raw = request.args.get(name)
    if raw is None or raw == '':
        n = default
    else:
        try:
            n = int(raw)
        except Exception:
            raise ValueError(f'{name} inválido')
    if min_v is not None and n < min_v:
        raise ValueError(f'{name} debe ser >= {min_v}')
    if max_v is not None and n > max_v:
        raise ValueError(f'{name} debe ser <= {max_v}')
    return n


def _float_q(name: str, default: float, min_v: Optional[float] = None, max_v: Optional[float] = None) -> float:
    raw = request.args.get(name)
    if raw is None or raw == '':
        x = default
    else:
        try:
            x = float(raw)
        except Exception:
            raise ValueError(f'{name} inválido')
    if min_v is not None and x < min_v:
        raise ValueError(f'{name} debe ser >= {min_v}')
    if max_v is not None and x > max_v:
        raise ValueError(f'{name} debe ser <= {max_v}')
    return x


def _json_error(msg: str, status: int = 400, **extra):
    payload = {'ok': False, 'error': msg}
    payload.update(extra)
    return jsonify(payload), status


def _require_token_if_configured():
    expected = (_env('DAEMON_TOKEN', '') or '').strip()
    if not expected:
        return None
    provided = (request.headers.get('X-DAEMON-TOKEN') or request.args.get('token') or '').strip()
    if provided != expected:
        return _json_error('Unauthorized', 401)
    return None


def _state_path() -> str:
    return _env('STATE_FILE', '/tmp/daemon_bounce_state.json')


def _load_state() -> Dict[str, Any]:
    return load_state(_state_path())


def _save_state(state: Dict[str, Any]) -> None:
    save_state(_state_path(), state)


def _notion_token() -> str:
    return (_env('NOTION_TOKEN', '') or '').strip()


def _notion_version() -> str:
    return (_env('NOTION_API_VERSION', '2022-06-28') or '2022-06-28').strip()


def _watch_page_id() -> str:
    return (_env('NOTION_WATCH_PAGE_ID', '') or '').strip()


def _watch_page_title() -> str:
    return (_env('NOTION_WATCH_PAGE_TITLE', 'test_bridge') or 'test_bridge').strip()


def _tick_forward_mode() -> str:
    """Modo de forwarding del tick:
    - first_url: reenvía la primera URL encontrada en Notion (comportamiento original)
    - relay:    ignora la URL como destino y dispara RELAY_TRIGGER_URL
                (usa la URL real solo para dedupe/trigger)
    """
    raw = (request.args.get('forward_mode') or _env('TICK_FORWARD_MODE', 'first_url') or 'first_url').strip().lower()
    if raw in ('relay', 'relay_url', 'trigger', 'trigger_url'):
        return 'relay'
    return 'first_url'


def _relay_trigger_url() -> str:
    return (request.args.get('relay_url') or _env('RELAY_TRIGGER_URL', '') or '').strip()


def _resolve_watch_page(*, timeout_sec: float) -> Dict[str, Any]:
    token = _notion_token()
    if not token:
        raise RuntimeError('Falta NOTION_TOKEN')

    page_id = _watch_page_id()
    if page_id:
        return {'page_id': normalize_page_id(page_id), 'page_title': None}

    title = _watch_page_title()
    page = search_page_by_title(token, title, timeout=timeout_sec, notion_version=_notion_version())
    if not page:
        raise RuntimeError(f'No se encontró página de Notion con título: {title}')
    return {'page_id': normalize_page_id(page.get('id') or ''), 'page_title': title}


def _first_valid_url(urls):
    for u in urls or []:
        if isinstance(u, str) and u.startswith(('http://', 'https://')):
            return u.strip()
    return None


def _page_sig(meta: Dict[str, Any], first_url: Optional[str]) -> str:
    src = f"{meta.get('page_id') or ''}|{meta.get('last_edited_time') or ''}|{first_url or ''}"
    return sha256_text(src)


def _cooldown_active(last_ts: Optional[int], cooldown_sec: int) -> bool:
    if not cooldown_sec or not last_ts:
        return False
    return (now_ts() - int(last_ts)) < cooldown_sec


def _public_state_view(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'ok': True,
        'state_file': _state_path(),
        'last_check': state.get('last_check'),
        'notion': state.get('notion', {}),
        'warmup': state.get('warmup', {}),
    }


def _forward_url(url: str, *, timeout_sec: float, max_hops: int, debug: bool) -> Dict[str, Any]:
    return forward_get(url, timeout_sec=timeout_sec, max_hops=max_hops)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.before_request
def _auth_gate():
    if request.path.endswith('/health'):
        return None
    return _require_token_if_configured()


@bp.get('/health')
def health():
    return jsonify(
        ok=True,
        service='daemon-bounce',
        version='1.1.0',
        now=now_ts(),
        has_notion_token=bool(_notion_token()),
        watch_page_id=bool(_watch_page_id()),
        watch_page_title=_watch_page_title(),
        tick_forward_mode=(_env('TICK_FORWARD_MODE', 'first_url') or 'first_url'),
        has_relay_trigger_url=bool((_env('RELAY_TRIGGER_URL', '') or '').strip()),
        state_file=_state_path(),
    )


@bp.get('/state')
def state_endpoint():
    state = _load_state()
    return jsonify(_public_state_view(state))


@bp.get('/bounce')
def bounce_endpoint():
    url = (request.args.get('url') or '').strip()
    if not url:
        return _json_error('Falta url', 400)
    try:
        timeout_sec = _float_q('timeout', float(_env('BOUNCE_TIMEOUT_SEC', '20')), min_v=0.5, max_v=120.0)
        max_hops = _int_q('max_hops', int(_env('BOUNCE_MAX_HOPS', '1')), min_v=1, max_v=10)
    except ValueError as e:
        return _json_error(str(e), 400)

    try:
        result = _forward_url(url, timeout_sec=timeout_sec, max_hops=max_hops, debug=True)
    except Exception as e:
        return _json_error(f'Bounce error: {e}', 502, url=url)
    return jsonify(ok=True, action='bounce', result=result)


@bp.get('/tick')
def tick():
    t0 = time.perf_counter()
    debug = _bool_q('debug', False)
    run = _bool_q('run', True)
    force = _bool_q('force', False)

    try:
        timeout_sec = _float_q('timeout', float(_env('NOTION_TIMEOUT_SEC', '15')), min_v=0.5, max_v=120.0)
        max_blocks = _int_q('max_blocks', int(_env('NOTION_MAX_BLOCKS', '500')), min_v=1, max_v=5000)
        max_hops = _int_q('max_hops', int(_env('BOUNCE_MAX_HOPS', '1')), min_v=1, max_v=10)
        cooldown_sec = _int_q('cooldown_sec', int(_env('TICK_COOLDOWN_SEC', '2')), min_v=0, max_v=86400)
        max_urls = _int_q('max_urls', 1, min_v=1, max_v=1)  # cola simple de 1
    except ValueError as e:
        return _json_error(str(e), 400)

    state = _load_state()
    state['last_check'] = now_ts()

    # cooldown para no disparar dos veces casi seguidas por cron duplicado
    last_triggered = state.get('notion', {}).get('last_result', {}).get('triggered_at') if isinstance(state.get('notion', {}).get('last_result'), dict) else None
    if (not force) and _cooldown_active(last_triggered, cooldown_sec):
        _save_state(state)
        payload = {
            'ok': True,
            'action': 'tick',
            'status': 'cooldown',
            'cooldown_sec': cooldown_sec,
            'last_triggered_at': last_triggered,
        }
        return jsonify(payload)

    try:
        page_ref = _resolve_watch_page(timeout_sec=timeout_sec)
        token = _notion_token()
        urls, meta = extract_urls_from_page(
            token,
            page_ref['page_id'],
            timeout=timeout_sec,
            notion_version=_notion_version(),
            max_blocks=max_blocks,
        )
        first_url = _first_valid_url(urls[:max_urls])
        sig = _page_sig(meta, first_url)
    except Exception as e:
        state.setdefault('notion', {})['last_error'] = str(e)
        _save_state(state)
        return _json_error(f'Notion tick error: {e}', 502)

    nst = state.setdefault('notion', {})
    nst['last_page_id'] = meta.get('page_id')
    nst['last_page_title'] = meta.get('page_title') or page_ref.get('page_title')
    nst['last_page_last_edited'] = meta.get('last_edited_time')
    nst['last_first_url'] = first_url
    nst['last_page_sig_seen'] = sig
    nst['last_error'] = None

    prev_forwarded_sig = nst.get('last_page_sig_forwarded')
    changed = sig != prev_forwarded_sig

    base_resp = {
        'ok': True,
        'action': 'tick',
        'run': run,
        'watch': {
            'page_id': meta.get('page_id'),
            'page_title': meta.get('page_title') or page_ref.get('page_title'),
            'last_edited_time': meta.get('last_edited_time'),
            'blocks_scanned': meta.get('blocks_scanned'),
        },
        'queue': {
            'mode': 'single',
            'first_url': first_url,
            'max_urls': max_urls,
            'urls_found_total': len(urls),
        },
        'changed': bool(changed),
        'force': bool(force),
    }

    trigger_mode = _tick_forward_mode()
    target_url = first_url
    if trigger_mode == 'relay':
        target_url = _relay_trigger_url()
        if debug:
            base_resp['trigger'] = {
                'mode': 'relay',
                'target_url': target_url or None,
                'source_url_for_dedupe': first_url,
            }
    elif debug:
        base_resp['trigger'] = {
            'mode': 'first_url',
            'target_url': first_url,
        }

    if not first_url:
        nst['last_result'] = {'status': 'no_url', 'checked_at': now_ts()}
        _save_state(state)
        base_resp['status'] = 'no_url'
        if not debug:
            # respuesta compacta para cron/logs
            return jsonify({'ok': True, 'status': 'no_url', 'changed': bool(changed)})
        base_resp['sample_urls'] = urls[:10]
        return jsonify(base_resp)

    if trigger_mode == 'relay' and not target_url:
        nst['last_error'] = 'Falta RELAY_TRIGGER_URL (o relay_url) para TICK_FORWARD_MODE=relay'
        nst['last_result'] = {'status': 'relay_target_missing', 'checked_at': now_ts()}
        _save_state(state)
        if not debug:
            return jsonify({'ok': False, 'status': 'relay_target_missing'}), 500
        base_resp['status'] = 'relay_target_missing'
        return jsonify(base_resp), 500

    if not changed and not force:
        nst['last_result'] = {'status': 'no_change', 'checked_at': now_ts()}
        _save_state(state)
        if not debug:
            return jsonify({'ok': True, 'status': 'no_change'})
        base_resp['status'] = 'no_change'
        return jsonify(base_resp)

    if not run:
        nst['last_result'] = {'status': 'dry_run', 'checked_at': now_ts(), 'url': target_url, 'source_first_url': first_url, 'trigger_mode': trigger_mode}
        _save_state(state)
        if not debug:
            return jsonify({'ok': True, 'status': 'dry_run', 'url': target_url, 'trigger_mode': trigger_mode})
        base_resp['status'] = 'dry_run'
        return jsonify(base_resp)

    try:
        fwd = _forward_url(target_url, timeout_sec=float(_env('BOUNCE_TIMEOUT_SEC', '20')), max_hops=max_hops, debug=debug)
        triggered_at = now_ts()
        nst['last_page_sig_forwarded'] = sig
        nst['last_result'] = {
            'status': 'forwarded',
            'triggered_at': triggered_at,
            'trigger_mode': trigger_mode,
            'source_first_url_hash': sha256_text(first_url or ''),
            'requested_url_hash': sha256_text(target_url or ''),
            'status_code': fwd.get('status_code'),
            'ok': fwd.get('ok'),
            'latency_ms': fwd.get('latency_ms'),
        }
        _save_state(state)
    except Exception as e:
        nst['last_error'] = str(e)
        nst['last_result'] = {'status': 'forward_error', 'checked_at': now_ts()}
        _save_state(state)
        return _json_error(f'Forward error: {e}', 502, url=target_url, trigger_mode=trigger_mode, source_first_url=first_url)

    if not debug:
        # compacto para cron. JSON mínimo.
        return jsonify({'ok': True, 'status': 'forwarded', 'code': fwd.get('status_code')})

    base_resp['status'] = 'forwarded'
    base_resp['forward_result'] = fwd
    base_resp['forwarded_to'] = target_url
    base_resp['duration_ms'] = int((time.perf_counter() - t0) * 1000)
    return jsonify(base_resp)


@bp.get('/warmup_tick')
def warmup_tick():
    """Dispara una URL de warmup configurable cada X segundos (pensado para Groq warmup)."""
    debug = _bool_q('debug', False)
    run = _bool_q('run', True)
    force = _bool_q('force', False)
    warmup_url = (request.args.get('url') or _env('GROQ_WARMUP_URL', '')).strip()
    if not warmup_url:
        return _json_error('Falta url o GROQ_WARMUP_URL', 400)

    try:
        cooldown_sec = _int_q('cooldown_sec', int(_env('GROQ_WARMUP_COOLDOWN_SEC', '1500')), min_v=0, max_v=86400)
        timeout_sec = _float_q('timeout', float(_env('BOUNCE_TIMEOUT_SEC', '20')), min_v=0.5, max_v=120.0)
        max_hops = _int_q('max_hops', int(_env('BOUNCE_MAX_HOPS', '1')), min_v=1, max_v=10)
    except ValueError as e:
        return _json_error(str(e), 400)

    state = _load_state()
    wst = state.setdefault('warmup', {})
    last_run = wst.get('last_run_at')
    if (not force) and _cooldown_active(last_run, cooldown_sec):
        _save_state(state)
        return jsonify({'ok': True, 'status': 'cooldown', 'last_run_at': last_run, 'cooldown_sec': cooldown_sec})

    if not run:
        return jsonify({'ok': True, 'status': 'dry_run', 'url': warmup_url, 'cooldown_sec': cooldown_sec})

    try:
        res = _forward_url(warmup_url, timeout_sec=timeout_sec, max_hops=max_hops, debug=debug)
    except Exception as e:
        wst['last_error'] = str(e)
        wst['last_status'] = 'error'
        _save_state(state)
        return _json_error(f'Warmup error: {e}', 502)

    wst['last_run_at'] = now_ts()
    wst['last_status'] = 'ok' if res.get('ok') else 'http_error'
    wst['last_latency_ms'] = res.get('latency_ms')
    wst['last_error'] = None
    _save_state(state)

    if not debug:
        return jsonify({'ok': True, 'status': wst['last_status'], 'code': res.get('status_code')})
    return jsonify({'ok': True, 'status': wst['last_status'], 'result': res})
