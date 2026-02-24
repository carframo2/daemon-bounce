from __future__ import annotations

import hashlib
import time
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="replace")).hexdigest()


def _with_bounce_hop(url: str, *, max_hops: int = 1) -> str:
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    hop = 0
    new_q = []
    for k, v in q:
        if k == "_bounce_hop":
            try:
                hop = int(v)
            except Exception:
                hop = 0
        else:
            new_q.append((k, v))
    if hop >= max_hops:
        raise ValueError(f"bounce hop limit reached ({hop} >= {max_hops})")
    new_q.append(("_bounce_hop", str(hop + 1)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(new_q, doseq=True), parts.fragment))


def forward_get(
    url: str,
    *,
    timeout_sec: float = 20.0,
    max_hops: int = 1,
    user_agent: str = "daemon-bounce/2.0",
    allow_redirects: bool = True,
) -> Dict[str, Any]:
    final_url = _with_bounce_hop(url, max_hops=max_hops)
    t0 = time.perf_counter()

    r = requests.get(
        final_url,
        timeout=timeout_sec,
        headers={"User-Agent": user_agent, "Cache-Control": "no-cache"},
        allow_redirects=allow_redirects,
    )

    dt = int((time.perf_counter() - t0) * 1000)
    content_type = r.headers.get("Content-Type", "")

    try:
        body_text = r.text
    except Exception:
        body_text = ""

    return {
        "requested_url": url,
        "forwarded_url": final_url,
        "status_code": r.status_code,
        "ok": bool(r.ok),
        "latency_ms": dt,
        "content_type": content_type,
        # 👇 IMPORTANTE: esto faltaba
        "body_text": body_text,
        "body_preview": (body_text[:500] if isinstance(body_text, str) else None),
        "body_len": (len(body_text) if isinstance(body_text, str) else None),
        "headers": {"content-type": content_type},
    }
