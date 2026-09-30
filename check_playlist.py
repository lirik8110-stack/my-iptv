#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка готового M3U: живость потоков и статистика по группам.

Запуск:
    python check_playlist.py playlist.m3u
    python check_playlist.py playlist.m3u --limit 200

Работает и без aiohttp — тогда проверяет живость пулом потоков.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_playlist import EXTINF, ATTR, UA  # noqa: E402


def load(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    items, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("#EXTINF"):
            m = EXTINF.match(line)
            if m:
                attrs = {}
                for k, v in ATTR.findall(m.group(2)):
                    attrs.setdefault(k.lower(), v)
                cur = {"name": m.group(3).strip(), "group": attrs.get("group-title", "-"), "url": ""}
            continue
        if line and not line.startswith("#"):
            if cur is None:
                cur = {"name": "?", "group": "-", "url": ""}
            cur["url"] = line
            items.append(cur)
            cur = None
    return items


async def check(items: list[dict], timeout: int = 8, concurrency: int = 64, limit: int = 0) -> list[bool]:
    target = items[:limit] if limit else items
    try:
        import aiohttp
    except ImportError:
        from concurrent.futures import ThreadPoolExecutor
        from build_playlist import check_url_sync
        print("(aiohttp нет — проверяю пулом потоков)")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(lambda it: check_url_sync(it["url"], timeout), target))

    sem = asyncio.Semaphore(concurrency)

    async def one(session, url: str) -> bool:
        async with sem:
            for method, hdr in (("HEAD", {}), ("GET", {"Range": "bytes=0-2047"})):
                try:
                    h = {"User-Agent": UA, **hdr}
                    kw = {"timeout": aiohttp.ClientTimeout(total=timeout),
                          "allow_redirects": True, "ssl": False, "headers": h}
                    async with session.request(method, url, **kw) as r:
                        if r.status in (200, 206, 301, 302, 303, 307, 308):
                            return True
                except Exception:
                    pass
            return False

    conn = aiohttp.TCPConnector(limit=concurrency + 8, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await asyncio.gather(*(one(session, it["url"]) for it in target))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    items = load(path)
    groups = Counter(it["group"] for it in items)
    hosts = Counter(re.sub(r"^[a-z]+://([^/:]+).*$", r"\1", it["url"]) for it in items)
    dups = len(items) - len({it["url"] for it in items})

    print(f"файл: {path}")
    print(f"каналов: {len(items)} | уникальных URL: {len(items) - dups} | дублей: {dups}")
    print(f"групп: {len(groups)}")
    print("\nтоп-15 групп:")
    for g, c in groups.most_common(15):
        print(f"  {c:>6}  {g}")
    print("\nтоп-15 хостов:")
    for h, c in hosts.most_common(15):
        print(f"  {c:>6}  {h}")

    res = asyncio.run(check(items, limit=limit))
    if res:
        alive = sum(res)
        print(f"\nживых: {alive} из {len(res)} ({alive * 100 // max(len(res), 1)}%)")
        dead_groups = Counter(items[i]["group"] for i, ok in enumerate(res) if not ok)
        print("больше всего мёртвых в группах:")
        for g, c in dead_groups.most_common(10):
            print(f"  {c:>6}  {g}")


if __name__ == "__main__":
    main()
