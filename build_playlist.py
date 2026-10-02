#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка одного M3U-плейлиста из нескольких источников.

Что делает:
  1. читает config.json
  2. качает источники (M3U/M3U8 или простой список ссылок)
  3. склеивает, чистит, убирает дубли
  4. по желанию проверяет живость каждого потока (параллельно)
  5. пишет playlist.m3u + статистику
  6. отказывается перезаписывать результат, если каналов подозрительно мало

Зависимости: только стандартная библиотека Python 3.9+
Запуск: python build_playlist.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("M3U_CONFIG") or os.path.join(ROOT, "config.json")
OUT = os.environ.get("M3U_OUT") or os.path.join(ROOT, "playlist.m3u")
# у каждого плейлиста свой файл статуса, иначе они будут перезаписывать друг друга
STATUS = os.environ.get("M3U_STATUS") or os.path.join(ROOT, "status.txt")

UA = "VLC/3.0.20 LibVLC/3.0.20"
EXTINF = re.compile(r"^#EXTINF:\s*(-?\d+(?:\.\d+)?)\s*(.*?),(.*)$")
ATTR = re.compile(r'([A-Za-z0-9_.\-]+)="([^"]*)"')


# ---------------------------------------------------------------- утилиты
def load_config() -> dict:
    with open(CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("sources"):
        sys.exit("config.json: список sources пуст — нечего собирать")
    return cfg


JUNK_GROUP = {"undefined", "none", "null", "general", "other", "прочее", "разное", "n/a", "-"}

UA_JUNK = re.compile(
    r'(?:like\s+Gecko|Mozilla/|AppleWebKit|Safari/\d|Chrome/\d|Firefox/\d|MSIE|Trident/|'
    r'group-title=|tvg-logo=|tvg-id=|#EXTINF)',
    re.IGNORECASE,
)

JUNK_URL = re.compile(
    r"^(?:https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[?::1\]?)(?::\d+)?(?:/|$)"
    r"|https?://(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"
    r"|https?://[^/]*:(?:0|1)/)"
    , re.IGNORECASE,
)

# Домены, которые отдают не поток, а заставку «смотрите в нашем приложении».
# Wink (бывшая Zabava) пускает чужие плееры только на свою заглушку, поэтому
# такие ссылки надо выбрасывать, даже если формально они «работают».
BLOCKED_URL_HOSTS = (
    "zabava-htlive.cdn.ngenix.net",
    "wink.ru",
    "cinerama.uz",
)


def is_blocked_url(url: str) -> bool:
    """Отсеивает ссылки сервисов, не отдающих поток сторонним плеерам."""
    u = (url or "").lower()
    return any(h in u for h in BLOCKED_URL_HOSTS)


def clean_name(raw: str) -> str:
    """В публичных плейлистах встречается мусор: имя канала склеено с User-Agent
    и вторым (битым) EXTINF. Пример реальной строки источника:

      #EXTINF:-1 tvg-id="X" ...,like Gecko Chrome/144 Safari/537.36" group-title="News",BTV

    Настоящее имя — после последней запятой.
    """
    name = (raw or "").strip()
    if name.count(",") >= 1 and UA_JUNK.search(name):
        name = name.rsplit(",", 1)[-1]
    m = UA_JUNK.search(name)
    if m and m.start() > 0:
        name = name[:m.start()]
    name = name.strip().strip('"').strip()
    name = re.sub(r"\s{2,}", " ", name)
    if not name:
        return "Канал"
    return name[:120]


def clean_group(raw: str) -> str:
    """Источники любят писать group-title="Кино;Спорт;HD" — берём первую осмысленную группу."""
    parts = [p.strip() for p in (raw or "").replace(",", ";").split(";")]
    for p in parts:
        if p and p.lower() not in JUNK_GROUP:
            return p
    return "Прочее"


def is_junk_url(url: str) -> bool:
    """Отсеиваем заведомо нерабочее: localhost, приватные диапазоны, порт 0."""
    u = (url or "").strip()
    if not u or not u.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://", "mms://")):
        return True
    return bool(JUNK_URL.match(u))


def absolutize(url: str, base: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://", "mms://")):
        return url
    if not base:
        return ""
    from urllib.parse import urljoin
    return urljoin(base, url)


# ---------------------------------------------------------------- загрузка
def http_get(url: str, timeout: int) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_playlist(text: str, url_base: str, default_group: str = "Прочее") -> list[dict]:
    """Понимает и #EXTM3U, и голый список ссылок."""
    text = text.replace("\ufeff", "").lstrip("\xef\xbb\xbf")
    if "#EXTM3U" not in text[:4096].upper():
        # простой список ссылок
        out = []
        for line in text.splitlines():
            u = line.strip()
            if u and not u.startswith("#"):
                out.append({"name": "Канал", "url": absolutize(u, url_base),
                            "group": default_group, "logo": "", "tvg_id": ""})
        return out

    items: list[dict] = []
    cur: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            m = EXTINF.match(line)
            if m:
                attrs: dict[str, str] = {}
                # при дублировании атрибутов оставляем ПЕРВОЕ значение — оно настоящее
                for k, v in ATTR.findall(m.group(2)):
                    attrs.setdefault(k.lower(), v)
                group = clean_group(attrs.get("group-title", ""))
                if group == "Прочее" and default_group != "Прочее":
                    # у источника нет нормальной группы — берём имя группы из config.json
                    group = default_group
                cur = {
                    "name": clean_name(m.group(3)),
                    "group": group,
                    "logo": absolutize(attrs.get("tvg-logo", ""), url_base),
                    "tvg_id": attrs.get("tvg-id", ""),
                    "url": "",
                }
            continue
        if line.startswith("#"):
            continue
        # это ссылка
        if cur is None:
            cur = {"name": "Канал", "group": default_group, "logo": "", "tvg_id": "", "url": ""}
        cur["url"] = absolutize(line, url_base)
        if cur["url"]:
            items.append(cur)
        cur = None
    return items


# ---------------------------------------------------------------- проверка
def check_url_sync(url: str, timeout: int) -> bool:
    """Проверка одного потока: сначала HEAD, потом GET с Range.

    Часть серверов не отвечает на HEAD и отдаёт 403 без User-Agent.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-2047"})):
        try:
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": UA, "Accept": "*/*", **extra})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                if r.status in (200, 206, 301, 302, 303, 307, 308):
                    return True
        except Exception:
            continue
    return False


def filter_alive_threaded(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Резервный путь без aiohttp: пул потоков. Работает на голом Python."""
    from concurrent.futures import ThreadPoolExecutor

    workers = int(cfg.get("concurrency", 64))
    timeout = int(cfg.get("timeout_sec", 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        flags = list(pool.map(lambda it: check_url_sync(it["url"], timeout), items))
    alive = [it for it, ok in zip(items, flags) if ok]
    return alive, len(items) - len(alive)


async def check_one(session, sem, url: str, timeout: int) -> bool:
    import aiohttp  # noqa: WPS433  (импорт внутри — чтобы работал fallback)

    async with sem:
        for method in ("HEAD", "GET"):
            try:
                kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout),
                          "allow_redirects": True,
                          "ssl": False,
                          "headers": {"User-Agent": UA}}
                if method == "GET":
                    kwargs["headers"] = {"User-Agent": UA, "Range": "bytes=0-1023"}
                async with session.request(method, url, **kwargs) as r:
                    if r.status in (200, 206, 301, 302, 303, 307, 308):
                        return True
                    if method == "GET":
                        return False
            except Exception:
                if method == "GET":
                    return False
        return False


async def filter_alive(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    try:
        import aiohttp
    except ImportError:
        print("  i aiohttp нет — проверяю живость пулом потоков (медленнее)")
        return await asyncio.get_running_loop().run_in_executor(
            None, filter_alive_threaded, items, cfg
        )

    sem = asyncio.Semaphore(int(cfg.get("concurrency", 64)))
    timeout = int(cfg.get("timeout_sec", 8))
    conn = aiohttp.TCPConnector(limit=int(cfg.get("concurrency", 64)) + 8, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        results = await asyncio.gather(
            *(check_one(session, sem, it["url"], timeout) for it in items)
        )
    alive = [it for it, ok in zip(items, results) if ok]
    return alive, len(items) - len(alive)


# ---------------------------------------------------------------- фильтры
def apply_filters(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    inc_g = {g.lower() for g in cfg.get("include_groups") or []}
    exc_g = {g.lower() for g in cfg.get("exclude_groups") or []}
    inc_n = re.compile(cfg["include_name_regex"]) if cfg.get("include_name_regex") else None
    exc_n = re.compile(cfg["exclude_name_regex"]) if cfg.get("exclude_name_regex") else None
    # белый список по именам: фиксирует набор, проверенный вручную
    allow_names = {n.strip().lower() for n in (cfg.get("include_names") or []) if n.strip()}

    out, junk = [], 0
    for it in items:
        if cfg.get("drop_junk_urls", True) and is_junk_url(it["url"]):
            junk += 1
            continue
        if not cfg.get("allow_blocked_hosts", False) and is_blocked_url(it["url"]):
            junk += 1
            continue
        if allow_names and it["name"].strip().lower() not in allow_names:
            continue
        g = it["group"].lower()
        if inc_g and g not in inc_g:
            continue
        if g in exc_g:
            continue
        if inc_n and not inc_n.search(it["name"]):
            continue
        if exc_n and exc_n.search(it["name"]):
            continue
        out.append(it)
    return out, junk


def normalize_quality(name: str) -> str:
    """Убирает технические пометки качества, чтобы найти дубли одного канала:
    «Первый (1080p)» и «Первый (576p)» — один и тот же канал."""
    n = re.sub(r"\((?:[^)]*\b(?:p|i|k|hd|fhd|uhd|sd|hevc|h265|h264|4k|8k)\b[^)]*)\)",
               " ", name, flags=re.IGNORECASE)
    n = re.sub(r"\b(?:hd|fhd|uhd|sd|hevc|h265|h264|4k|8k)\b", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"[\s\-–—|·•]+", " ", n)
    return n.strip().lower()


def dedupe(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Убирает дубли по ссылке, по паре «имя+группа» и, если включено,
    по нормализованному имени канала.

    dedupe_across_groups = true — канал остаётся только в одной (первой по
    приоритету) группе, а не дублируется в «Кино» и «Общих» одновременно.
    """
    by_name = bool(cfg.get("dedupe_by_name", False))
    cross = bool(cfg.get("dedupe_across_groups", False))
    seen_url, seen_key, seen_name, out = set(), set(), set(), []
    for it in items:
        u = it["url"]
        key = (it["name"].lower().strip(), it["group"].lower().strip())
        if u in seen_url or key in seen_key:
            continue
        if by_name or cross:
            nk = (normalize_quality(it["name"]),)
            if not cross:
                nk = (normalize_quality(it["name"]), it["group"].lower().strip())
            if nk in seen_name:
                continue
            seen_name.add(nk)
        seen_url.add(u)
        seen_key.add(key)
        out.append(it)
    return out, len(items) - len(out)


def normalize_group_name(group: str, cfg: dict) -> str:
    """Сводит группы-двойники к одному имени: «Региoнальные» -> «Региональные»."""
    mapping = cfg.get("group_aliases") or {}
    gl = group.strip().lower()
    for canonical, variants in mapping.items():
        if gl == canonical.strip().lower() or any(gl == v.strip().lower() for v in variants):
            return canonical
    return group


def force_group(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Собирает все каналы в одну группу.

    Нужно для персональных плейлистов: без этого каналы разбросаны по группам
    вида «212.15», «HARTUM TV SRL», «ПОМОЙКА» — это названия провайдерских
    серверов-источников, а не категории телевидения.
    """
    name = (cfg.get("force_group") or "").strip()
    if not name:
        return items, 0
    changed = sum(1 for it in items if it["group"] != name)
    for it in items:
        it["group"] = name
    return items, changed


def quality_rank(it: dict, order_idx: int) -> tuple:
    """Косвенная оценка надёжности канала — без сетевых проверок.

    Сортировка идёт по: (1) группа из order_groups, (2) косвенные признаки,
    (3) порядок источника. Основной признак — наличие логотипа: источники дают
    его нормальным каналам, а «затычкам» и мёртвым вставкам — нет.
    """
    name = it["name"]
    bad = 0
    if not it.get("logo"):
        bad += 1
    if re.search(r"(?i)\b(vpn|proxy|зеркало|backup|резерв)\b", name):
        bad += 2
    if len(name) < 3:
        bad += 2
    return (order_idx, bad)


def load_direct(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Добавляет постоянные ссылки из конфига (direct_links).

    Нужно для каналов, у которых подходящая ссылка лежит не в общем потоке
    источников, а прописана вручную и проверена. Такие ссылки не отбрасываются
    фильтрами: они уже отобраны.
    """
    direct = cfg.get("direct_links") or []
    added = 0
    for entry in direct:
        name = (entry.get("name") or "").strip()
        url = (entry.get("url") or "").strip()
        if not name or not url:
            continue
        items.append({
            "name": name,
            "group": (entry.get("group") or cfg.get("force_group") or "Прочее"),
            "logo": entry.get("logo", ""),
            "tvg_id": entry.get("tvg_id", ""),
            "url": url,
        })
        added += 1
    return items, added


def postprocess(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Группирует варианты одного канала и оставляет несколько рабочих ссылок.

    Без этого в плейлист попадают все повторы канала со всех провайдерских
    серверов: «НТВ» может встретиться 19 раз вперемешку с «НТВ Хит».
    С включённым postprocess на канал остаётся до max_per_channel ссылок,
    а резервные получают пометку «(резерв 2)», «(резерв 3)».
    """
    group_key = cfg.get("postprocess_group_key")
    if not group_key:
        return items, 0

    max_per = int(cfg.get("max_per_channel", 3) or 3)
    exclude_names = {n.strip().lower() for n in (cfg.get("exclude_exact_names") or []) if n.strip()}
    merged: dict[str, list[dict]] = {}
    order: list[str] = []
    for it in items:
        if it["name"].strip().lower() in exclude_names:
            continue
        key = (normalize_quality(it["name"]) or it["name"].lower().strip())
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(it)

    out: list[dict] = []
    before = len(items)
    for key in order:
        variants = merged[key]
        canonical = variants[0]["name"]
        for i, it in enumerate(variants[:max_per]):
            copy = dict(it)
            copy["name"] = canonical if i == 0 else f"{canonical} (резерв {i + 1})"
            out.append(copy)
    return out, before - len(out)


def sort_items(items: list[dict], cfg: dict) -> list[dict]:
    order = [g.lower() for g in cfg.get("order_groups") or []]

    def key(it):
        g = it["group"].lower()
        gi = order.index(g) if g in order else len(order)
        if cfg.get("quality_sort", False):
            return quality_rank(it, gi) + (it["group"].lower(), it["name"].lower())
        return (gi, it["group"].lower(), it["name"].lower())

    return sorted(items, key=key)


def limit_items(items: list[dict], cfg: dict) -> tuple[list[dict], int]:
    """Обрезает список до листаемого на телевизоре размера.

    Порядок источников в config.json = порядок приоритета, поэтому в каждой
    группе остаются первые (самые «главные») каналы.
    """
    per_group = int(cfg.get("max_per_group", 0) or 0)
    total = int(cfg.get("max_total", 0) or 0)
    before = len(items)

    if per_group:
        counts: dict[str, int] = {}
        kept = []
        for it in items:
            g = it["group"]
            if counts.get(g, 0) < per_group:
                counts[g] = counts.get(g, 0) + 1
                kept.append(it)
        items = kept

    if total and len(items) > total:
        # сохраняем пропорции между группами, а не срезаем хвост целиком
        per = max(1, total // max(len({i["group"] for i in items}), 1))
        counts = {}
        kept = []
        for it in items:
            g = it["group"]
            if counts.get(g, 0) < per:
                counts[g] = counts.get(g, 0) + 1
                kept.append(it)
        items = kept[:total]

    return items, before - len(items)


def proxyfy(url: str, base: str) -> str:
    """Если задан url_base — подменяем хост ссылки на свой (обход блокировок/гео).

    Пустое значение или незаполненный шаблон из config.json = ссылку не трогаем.
    """
    base = (base or "").strip()
    if not base or base.startswith(("ваш", "your")) or "example" in base:
        return url
    from urllib.parse import urlsplit, urlunsplit
    b = urlsplit(base)
    u = urlsplit(url)
    if not b.netloc or not u.netloc:
        return url
    # берём схему+хост от прокси, путь и query — от оригинала
    return urlunsplit((b.scheme or u.scheme, b.netloc, u.path, u.query, u.fragment))


# ---------------------------------------------------------------- запись
def build_text(items: list[dict], cfg: dict) -> str:
    """Собирает содержимое плейлиста БЕЗ отметки времени.

    Это важно: файл должен меняться только тогда, когда реально изменился
    список каналов. Иначе бот в облаке будет коммитить каждые 30 минут и
    раздувать репозиторий.
    """
    base = (cfg.get("url_base") or "").strip()
    lines = ['#EXTM3U x-tvg-url="https://iptv-org.github.io/epg/index.xml"']
    for it in items:
        url = proxyfy(it["url"], base)
        attrs = f' group-title="{it["group"]}"'
        if it.get("tvg_id"):
            attrs += f' tvg-id="{it["tvg_id"]}"'
        if it.get("logo"):
            attrs += f' tvg-logo="{it["logo"]}"'
        lines.append(f'#EXTINF:-1{attrs},{it["name"]}')
        lines.append(url)
    return "\n".join(lines) + "\n"


def write_atomic(path: str, text: str) -> None:
    """Пишет файл через временный и подменяет — файл не бывает «половинным»."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_playlist(items: list[dict], cfg: dict, status: str = "") -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 1. основной файл: только каналы, никакой изменчивой даты
    write_atomic(OUT, build_text(items, cfg))

    # 2. строка статуса живёт отдельно: её перезапись не создаёт истории в git.
    #    Неудача здесь не должна валить сборку — плейлист уже записан.
    if status:
        try:
            write_atomic(STATUS,
                         f"файл: {os.path.basename(OUT)}\nобновлено: {stamp}\nканалов: {len(items)}\n\nисточники:\n{status}\n")
        except Exception as e:  # noqa: BLE001
            print(f"  i {os.path.basename(STATUS)} записать не удалось ({e}) — это не критично")


# ---------------------------------------------------------------- main
async def main() -> None:
    cfg = load_config()
    t0 = time.time()
    report = []

    raw: list[dict] = []
    default_groups = cfg.get("source_groups") or []
    for idx, src in enumerate(cfg["sources"]):
        dg = default_groups[idx] if idx < len(default_groups) and default_groups[idx] else "Прочее"
        try:
            text = http_get(src, int(cfg.get("timeout_sec", 8)) * 3)
            got = parse_playlist(text, src, dg)
            print(f"[ok]   {len(got):>6} каналов <- {src}")
            report.append(f"ok   {len(got):>6}  {src}")
            raw.extend(got)
        except Exception as e:  # noqa: BLE001
            print(f"[fail] {e} <- {src}")
            report.append(f"FAIL       {src} :: {e}")

    if not raw:
        sys.exit("Ни один источник не загрузился — плейлист не изменён")

    items, junk = apply_filters(raw, cfg)
    for it in items:
        it["group"] = normalize_group_name(it["group"], cfg)
    items, dup = dedupe(items, cfg)
    items, merged = postprocess(items, cfg)
    items, regrouped = force_group(items, cfg)
    note = f", объединено вариантов {merged}" if merged else ""
    if regrouped:
        note += f", группа сведена к «{cfg.get('force_group')}»"
    print(f"после фильтров и дедупликации: {len(items)} "
          f"(мусорных ссылок: {junk}, дублей: {dup}{note})")

    dead = 0
    if cfg.get("only_alive"):
        max_check = int(cfg.get("alive_check_limit", 0))
        target = items[:max_check] if max_check else items
        print(f"проверяю {len(target)} потоков из {len(items)}...")
        alive, dead = await filter_alive(target, cfg)
        if max_check and max_check < len(items):
            # непроверенный хвост оставляем как есть, но помечаем
            items = alive + items[max_check:]
        else:
            items = alive
        print(f"живых: {len(alive)}, отброшено: {dead}")

    items = sort_items(items, cfg)
    items, direct_added = load_direct(items, cfg)
    if direct_added:
        # постоянные ссылки могли продублировать уже найденные — убираем повторы
        items, dup2 = dedupe(items, {"dedupe_by_name": True})
        dup += dup2
        items = sort_items(items, cfg)
        print(f"добавлено постоянных ссылок из конфига: {direct_added}"
              + (f", убрано повторов {dup2}" if dup2 else ""))
    items, cut = limit_items(items, cfg)
    if cut:
        print(f"сокращено до листаемого размера: убрано {cut}, осталось {len(items)}")

    minimum = int(cfg.get("min_channels", 1))
    if len(items) < minimum:
        sys.exit(f"ОТКАЗ: получилось {len(items)} каналов < min_channels={minimum}. "
                 f"Старый playlist.m3u оставлен как есть.")

    report.append(f"итог: {len(items)} каналов, дублей убрано {dup}, мусорных ссылок {junk}, "
                  f"сокращено {cut}, мёртвых отброшено {dead}, {time.time() - t0:.1f} с")
    write_playlist(items, cfg, status="\n".join(report))
    print(f"готово: {OUT} ({len(items)} каналов)")

    # экспорт проверенного списка имён: позволяет зафиксировать набор каналов,
    # проверенных вручную, в отдельном конфиге (include_names)
    if cfg.get("export_include_names"):
        names = sorted({it["name"] for it in items})
        print("\n--- INCLUDE_NAMES_JSON ---")
        print(json.dumps(names, ensure_ascii=False, indent=2))
        print("--- END INCLUDE_NAMES_JSON ---")


if __name__ == "__main__":
    asyncio.run(main())

