from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

NOTION_API_BASE = 'https://api.notion.com/v1'
DEFAULT_NOTION_API_VERSION = '2022-06-28'
URL_RE = re.compile(r'https?://[^\s<>"\]\)]+')

RICH_TEXT_TYPES = {
    'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
    'numbered_list_item', 'to_do', 'toggle', 'quote', 'callout', 'code'
}


def notion_headers(token: str, version: Optional[str] = None) -> Dict[str, str]:
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': (version or os.environ.get('NOTION_API_VERSION') or DEFAULT_NOTION_API_VERSION),
        'Content-Type': 'application/json',
    }


def _req(method: str, path: str, token: str, *, json_body=None, timeout: float = 15.0, notion_version: Optional[str] = None):
    url = f'{NOTION_API_BASE}{path}'
    r = requests.request(method, url, headers=notion_headers(token, notion_version), json=json_body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def normalize_page_id(page_id: str) -> str:
    s = (page_id or '').strip()
    if not s:
        return s
    return s.replace('-', '')


def extract_page_title(page_obj: Dict[str, Any]) -> str:
    props = page_obj.get('properties') or {}
    for _, prop in props.items():
        if isinstance(prop, dict) and prop.get('type') == 'title':
            arr = prop.get('title') or []
            return ''.join((x.get('plain_text') or '') for x in arr).strip()
    title_arr = page_obj.get('title') or []
    if isinstance(title_arr, list):
        return ''.join((x.get('plain_text') or '') for x in title_arr).strip()
    return ''


def search_page_by_title(token: str, title: str, *, timeout: float = 15.0, notion_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    title = (title or '').strip()
    if not title:
        return None
    payload = {
        'query': title,
        'filter': {'property': 'object', 'value': 'page'},
        'sort': {'direction': 'descending', 'timestamp': 'last_edited_time'},
    }
    data = _req('POST', '/search', token, json_body=payload, timeout=timeout, notion_version=notion_version)
    results = data.get('results') or []
    exact = []
    contains = []
    for page in results:
        t = extract_page_title(page)
        if not t:
            continue
        if t == title:
            exact.append(page)
        elif title.lower() in t.lower():
            contains.append(page)
    if exact:
        return exact[0]
    if contains:
        return contains[0]
    return results[0] if results else None


def get_page(token: str, page_id: str, *, timeout: float = 15.0, notion_version: Optional[str] = None) -> Dict[str, Any]:
    page_id = normalize_page_id(page_id)
    return _req('GET', f'/pages/{page_id}', token, timeout=timeout, notion_version=notion_version)


def iter_block_children(token: str, block_id: str, *, timeout: float = 15.0, notion_version: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    block_id = normalize_page_id(block_id)
    start_cursor = None
    while True:
        path = f'/blocks/{block_id}/children?page_size=100'
        if start_cursor:
            path += f'&start_cursor={start_cursor}'
        data = _req('GET', path, token, timeout=timeout, notion_version=notion_version)
        for b in data.get('results') or []:
            yield b
        if not data.get('has_more'):
            break
        start_cursor = data.get('next_cursor')
        if not start_cursor:
            break


def walk_blocks_dfs(token: str, root_block_id: str, *, timeout: float = 15.0, notion_version: Optional[str] = None, max_blocks: int = 500) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stack: List[str] = [normalize_page_id(root_block_id)]
    while stack and len(out) < max_blocks:
        current = stack.pop()
        try:
            children = list(iter_block_children(token, current, timeout=timeout, notion_version=notion_version))
        except Exception:
            continue
        for child in children:
            out.append(child)
            if len(out) >= max_blocks:
                break
            if child.get('has_children'):
                cid = child.get('id')
                if cid:
                    stack.append(cid)
    return out


def _extract_urls_from_rich_text_items(items: List[Dict[str, Any]]) -> List[str]:
    urls: List[str] = []
    for rt in items or []:
        href = rt.get('href')
        if isinstance(href, str) and href.startswith(('http://', 'https://')):
            urls.append(href)
        txt = rt.get('plain_text') or ''
        if txt:
            urls.extend(URL_RE.findall(txt))
        ann = rt.get('text') or {}
        link = (ann.get('link') or {}).get('url') if isinstance(ann, dict) else None
        if isinstance(link, str) and link.startswith(('http://', 'https://')):
            urls.append(link)
    return urls


def extract_urls_from_block(block: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    btype = block.get('type')
    if not btype:
        return urls

    payload = block.get(btype) or {}

    if btype in RICH_TEXT_TYPES:
        urls.extend(_extract_urls_from_rich_text_items(payload.get('rich_text') or []))

    # files/images/bookmarks/embed/link_preview/etc.
    for key in ('url', 'caption'):
        val = payload.get(key)
        if isinstance(val, str) and val.startswith(('http://', 'https://')):
            urls.append(val)
        elif isinstance(val, list):
            urls.extend(_extract_urls_from_rich_text_items(val))

    if btype in ('image', 'video', 'audio', 'file', 'pdf'):
        t = payload.get('type')
        if t in ('external', 'file'):
            u = (payload.get(t) or {}).get('url')
            if isinstance(u, str) and u.startswith(('http://', 'https://')):
                urls.append(u)

    # fallback: cualquier string del JSON del bloque
    def walk(obj: Any):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, str):
            urls.extend(URL_RE.findall(obj))

    walk(payload)

    # dedupe preserving order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_urls_from_page(token: str, page_id: str, *, timeout: float = 15.0, notion_version: Optional[str] = None, max_blocks: int = 500) -> Tuple[List[str], Dict[str, Any]]:
    page = get_page(token, page_id, timeout=timeout, notion_version=notion_version)
    blocks = walk_blocks_dfs(token, page_id, timeout=timeout, notion_version=notion_version, max_blocks=max_blocks)
    urls: List[str] = []
    for b in blocks:
        urls.extend(extract_urls_from_block(b))
    seen = set()
    ordered = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    meta = {
        'page_id': page.get('id'),
        'page_title': extract_page_title(page),
        'last_edited_time': page.get('last_edited_time'),
        'blocks_scanned': len(blocks),
    }
    return ordered, meta
