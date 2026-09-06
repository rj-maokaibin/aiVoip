#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import ssl
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

_NEEDLES = (
    "/cgi-bin/luci/api/auth",
    "isCheckReadAgreement",
    "encry",
    "pwd",
    "encrypt",
    "AES",
    "RSA",
)
_ASSET_RE = re.compile(r"<(?:script|link)\b[^>]*(?:src|href)=[\"']([^\"']+)[\"']", re.I)
_JS_REF_RE = re.compile(
    r"[\"']([^\"'<>\s?#]+\.(?:js|mjs)(?:\?[^\"']*)?)[\"']",
    re.I,
)
_META_REFRESH_RE = re.compile(
    r"<meta\b[^>]*http-equiv=[\"']?refresh[\"']?[^>]*content=[\"'][^\"']*url=([^\"';>]+)",
    re.I,
)


def _fetch(url: str, *, timeout: float, max_bytes: int) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": "VOIP-Automation-ReadOnly-Source-Probe/2"})
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urlopen(request, timeout=timeout, context=context) as response:
        final_url = response.geturl()
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RuntimeError(f"WEB_SOURCE_ASSET_TOO_LARGE:{url}")
    return raw.decode("utf-8", errors="replace"), final_url


def _same_origin(base_url: str, candidate: str) -> bool:
    base = urlsplit(base_url)
    other = urlsplit(candidate)
    return (other.scheme, other.netloc) == (base.scheme, base.netloc)


def _contexts(text: str, needle: str, *, radius: int = 500, limit: int = 8) -> list[str]:
    found: list[str] = []
    start = 0
    lower = text.lower()
    target = needle.lower()
    while len(found) < limit:
        index = lower.find(target, start)
        if index < 0:
            break
        left = max(0, index - radius)
        right = min(len(text), index + len(needle) + radius)
        found.append(text[left:right])
        start = index + max(1, len(needle))
    return found


def _discover_same_origin_assets(base_url: str, page_url: str, text: str) -> list[str]:
    raw_refs: list[str] = []
    raw_refs.extend(_ASSET_RE.findall(text))
    raw_refs.extend(_JS_REF_RE.findall(text))
    raw_refs.extend(_META_REFRESH_RE.findall(text))

    discovered: list[str] = []
    for raw in raw_refs:
        candidate = urljoin(page_url, raw.strip())
        if _same_origin(base_url, candidate) and candidate not in discovered:
            discovered.append(candidate)
    return discovered


def probe(
    base_url: str,
    *,
    timeout: float = 10.0,
    max_assets: int = 80,
    max_bytes: int = 5_000_000,
) -> dict:
    base_url = base_url.rstrip("/") + "/"
    queue: deque[str] = deque([base_url])
    seen: set[str] = set()
    assets: list[dict] = []
    discovery: list[dict] = []

    while queue and len(seen) < max_assets:
        requested_url = queue.popleft()
        if requested_url in seen:
            continue
        seen.add(requested_url)

        try:
            text, final_url = _fetch(requested_url, timeout=timeout, max_bytes=max_bytes)
        except Exception as exc:
            assets.append({
                "url": requested_url,
                "error": type(exc).__name__,
                "matches": {},
            })
            continue

        if not _same_origin(base_url, final_url):
            assets.append({
                "url": requested_url,
                "final_url": final_url,
                "error": "CROSS_ORIGIN_REDIRECT_BLOCKED",
                "matches": {},
            })
            continue

        refs = _discover_same_origin_assets(base_url, final_url, text)
        discovery.append({
            "url": requested_url,
            "final_url": final_url,
            "discovered_assets": refs,
        })
        for candidate in refs:
            if candidate not in seen and candidate not in queue and len(seen) + len(queue) < max_assets:
                queue.append(candidate)

        matches = {needle: _contexts(text, needle) for needle in _NEEDLES}
        matches = {needle: contexts for needle, contexts in matches.items() if contexts}
        if matches or requested_url == base_url:
            assets.append({
                "url": requested_url,
                "final_url": final_url,
                "bytes": len(text.encode("utf-8")),
                "matches": matches,
            })

    return {
        "schema": "current-web-auth-source-probe-v2",
        "base_origin": f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}",
        "authenticated": False,
        "credentials_used": False,
        "request_method": "GET_ONLY",
        "same_origin_only": True,
        "needles": list(_NEEDLES),
        "asset_count_examined": len(seen),
        "matched_assets": assets,
        "discovery": discovery,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    payload = probe(args.base_url, timeout=args.timeout)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    matched = sum(1 for item in payload["matched_assets"] if item.get("matches"))
    print(
        "WEB_AUTH_SOURCE_PROBE=PASS "
        f"assets={payload['asset_count_examined']} matched={matched}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
