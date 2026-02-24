from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict


def _default_state() -> Dict[str, Any]:
    return {
        "last_check": None,
        "notion": {
            "last_page_sig_seen": None,
            "last_page_sig_forwarded": None,
            "last_page_id": None,
            "last_page_title": None,
            "last_page_last_edited": None,
            "last_first_url": None,
            "last_result": None,
            "last_error": None,
        },
        "warmup": {
            "last_run_at": None,
            "last_status": None,
            "last_latency_ms": None,
            "last_error": None,
        },
    }


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_state()
        base = _default_state()
        # merge shallowly
        base.update({k: v for k, v in data.items() if k in base and not isinstance(base[k], dict)})
        for k in ("notion", "warmup"):
            if isinstance(data.get(k), dict):
                base[k].update(data[k])
        return base
    except FileNotFoundError:
        return _default_state()
    except Exception:
        return _default_state()


def save_state(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='state_', suffix='.json', dir=os.path.dirname(path) or '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def now_ts() -> int:
    return int(time.time())
