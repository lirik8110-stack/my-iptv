#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генератор QR-кода со ссылкой на плейлист (чистый Python + Pillow).

Зачем: на телевизоре ссылку приходится вводить пультом, а она длинная.
Многие IPTV-плееры (Televizo, OTT Navigator, IPTV Smarters) умеют добавлять
плейлист по QR-коду: показывают его на экране ТВ, вы сканируете телефоном.

Использование:
    python make_qr.py "https://raw.githubusercontent.com/логин/репо/main/playlist.m3u"
    python make_qr.py "<ссылка>" --out qr.png

Результат: qr.png рядом со скриптом плюс QR прямо в консоли.
"""

from __future__ import annotations

import os
import sys

from PIL import Image

# ---------------------------------------------------------------- таблицы QR
# Общее число кодовых слов по версиям (данные + коррекция). Равно (модулей данных)/8.
TOTAL_CODEWORDS = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134, 6: 172,
                   7: 196, 8: 242, 9: 292, 10: 346}

# Число блоков коррекции по версии и уровню (L, M, Q, H)
BLOCKS = {
    1:  {"L": 1, "M": 1, "Q": 1, "H": 1},
    2:  {"L": 1, "M": 1, "Q": 1, "H": 1},
    3:  {"L": 1, "M": 1, "Q": 2, "H": 2},
    4:  {"L": 1, "M": 2, "Q": 2, "H": 4},
    5:  {"L": 1, "M": 2, "Q": 4, "H": 4},
    6:  {"L": 2, "M": 4, "Q": 4, "H": 4},
    7:  {"L": 2, "M": 4, "Q": 6, "H": 5},
    8:  {"L": 2, "M": 2, "Q": 4, "H": 6},
    9:  {"L": 2, "M": 3, "Q": 5, "H": 8},
    10: {"L": 2, "M": 4, "Q": 6, "H": 8},
}

# Число кодовых слов коррекции НА БЛОК — точные значения стандарта
ECC_PER_BLOCK = {
    1:  {"L": 7,  "M": 10, "Q": 13, "H": 17},
    2:  {"L": 10, "M": 16, "Q": 22, "H": 28},
    3:  {"L": 15, "M": 26, "Q": 18, "H": 22},
    4:  {"L": 20, "M": 18, "Q": 26, "H": 16},
    5:  {"L": 26, "M": 24, "Q": 18, "H": 22},
    6:  {"L": 18, "M": 16, "Q": 24, "H": 28},
    7:  {"L": 20, "M": 18, "Q": 18, "H": 26},
    8:  {"L": 24, "M": 22, "Q": 22, "H": 26},
    9:  {"L": 30, "M": 22, "Q": 20, "H": 24},
    10: {"L": 18, "M": 26, "Q": 24, "H": 28},
}

# (A, B) для формулы размещения модулей синхронизации
ALIGN_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
}

# Уровни коррекции
ECC_LEVELS = {"L": 0, "M": 1, "Q": 2, "H": 3}


# ---------------------------------------------------------------- GF(256)
def gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


GF_EXP, GF_LOG = gf_tables()


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def rs_generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        new = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            new[j] ^= gf_mul(c, 1)
            new[j + 1] ^= gf_mul(c, GF_EXP[i])
        poly = new
    return poly


def rs_encode(data: list[int], ecc_len: int) -> list[int]:
    gen = rs_generator(ecc_len)
    res = list(data) + [0] * ecc_len
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= gf_mul(g, coef)
    return res[len(data):]


# ---------------------------------------------------------------- сборка QR
def bits_to_bytes(bits: list[int]) -> list[int]:
    """Упаковывает поток битов в байты: старший бит первым."""
    out = []
    for i in range(0, len(bits) - 7, 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return out


def data_codewords(version: int, ecc: str) -> int:
    """Число кодовых слов, доступных под данные: всего минус коррекция."""
    n_blocks = BLOCKS[version][ecc]
    return TOTAL_CODEWORDS[version] - n_blocks * ECC_PER_BLOCK[version][ecc]


def pick_version(n_bytes: int, ecc: str) -> int:
    for v in range(1, 11):
        need_bits = 4 + (8 if v <= 9 else 16) + n_bytes * 8
        if need_bits <= data_codewords(v, ecc) * 8:
            return v
    raise ValueError("ссылка слишком длинная для этой версии генератора (максимум ~20 версия)")


def encode_data(payload: bytes, version: int, ecc: str) -> list[int]:
    bits: list[int] = []

    def push(value: int, n: int) -> None:
        """Добавляет число в поток битов, старший бит первым (как требует стандарт)."""
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                       # режим: байты
    push(len(payload), 8 if version <= 9 else 16)

    # ВАЖНО: заголовок занимает 12 бит (или 20), поэтому данные нужно выровнять
    # по границе байта — иначе каждый символ «переезжает» на полбайта.
    while len(bits) % 8:
        bits.append(0)

    for byte in payload:
        push(byte, 8)

    total_bits = data_codewords(version, ecc) * 8
    bits += [0] * min(4, total_bits - len(bits))          # терминатор
    while len(bits) % 8:
        bits.append(0)
    data = bits_to_bytes(bits)
    pad = [0xEC, 0x11]
    i = 0
    while len(data) < data_codewords(version, ecc):
        data.append(pad[i % 2])
        i += 1
    return data


def interleave(data: list[int], version: int, ecc: str) -> list[int]:
    n_blocks = BLOCKS[version][ecc]
    ecc_len = ECC_PER_BLOCK[version][ecc]
    total_data = data_codewords(version, ecc)
    short_len = total_data // n_blocks
    n_long = total_data % n_blocks          # блоков на 1 байт длиннее
    blocks, pos = [], 0
    for b in range(n_blocks):
        size = short_len + (1 if b >= n_blocks - n_long else 0)
        chunk = data[pos:pos + size]
        pos += size
        blocks.append((chunk, rs_encode(chunk, ecc_len)))

    out: list[int] = []
    for i in range(max(len(c) for c, _ in blocks)):
        for c, _ in blocks:
            if i < len(c):
                out.append(c[i])
    for i in range(ecc_len):
        for _, e in blocks:
            out.append(e[i])
    return out


def new_matrix(size: int):
    return [[None] * size for _ in range(size)]


def place_function_patterns(m, version: int) -> None:
    """Размечает ВСЕ служебные модули: маркеры, синхронизацию, выравнивание,
    зоны формата и версии. Всё, что осталось None — модули данных."""
    size = len(m)

    def finder(r0, c0):
        # 7x7 маркер поиска
        for dr in range(7):
            for dc in range(7):
                edge = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                m[r0 + dr][c0 + dc] = int(edge or core)

    # разделители: полосы вокруг маркеров (внутри кода их нет)
    def separators():
        for i in range(8):
            m[7][i] = 0        # под левым верхним
            m[i][7] = 0
            m[7][size - 1 - i] = 0   # под правым верхним
            m[size - 8][i] = 0       # над левым нижним

    # сначала обнуляем всё как «служебное», потом расставляем узоры
    for r in range(size):
        for c in range(size):
            m[r][c] = None

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # разделители вокруг маркеров поиска:
    # у верхнего левого — нижняя строка и правая колонка,
    # у верхнего правого — нижняя строка и ЛЕВАЯ колонка,
    # у нижнего левого — ВЕРХНЯЯ строка и правая колонка.
    for i in range(8):
        m[7][i] = 0                      # верхний левый: низ
        m[i][7] = 0                      # верхний левый: право
        m[7][size - 1 - i] = 0           # верхний правый: низ
        m[i][size - 8] = 0               # верхний правый: лево
        m[size - 8][i] = 0               # нижний левый: верх
        m[size - 1 - i][7] = 0           # нижний левый: право

    # синхронизация (между маркерами)
    for i in range(8, size - 8):
        m[6][i] = int(i % 2 == 0)
        m[i][6] = int(i % 2 == 0)

    # выравнивание
    for r in ALIGN_POS.get(version, []):
        for c in ALIGN_POS[version]:
            if (r < 9 and c < 9) or (r < 9 and c > size - 10) or (r > size - 10 and c < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = int(max(abs(dr), abs(dc)) != 1)

    # зона формата: 15 модулей у верхнего левого маркера
    for i in range(6):
        m[8][i] = 0
    m[8][7] = 0
    m[8][8] = 0
    m[7][8] = 0
    for i in range(9, 15):
        m[14 - i][8] = 0
    # и его дубликат у правого верхнего и левого нижнего
    for i in range(8):
        m[8][size - 1 - i] = 0
        m[size - 1 - i][8] = 0
    m[size - 8][8] = 1                 # тёмный модуль

    # информация о версии (от 7-й)
    if version >= 7:
        info = version << 12
        rem = info
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        info |= rem & 0xFFF
        for i in range(18):
            bit = (info >> i) & 1
            m[i // 3][size - 11 + i % 3] = bit
            m[size - 11 + i % 3][i // 3] = bit


def reserved_mask(version: int) -> list[list[bool]]:
    size = 17 + 4 * version
    m = new_matrix(size)
    place_function_patterns(m, version)
    return [[v is not None for v in row] for row in m]


def place_data(m, codewords: list[int]) -> None:
    size = len(m)
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):        # старший бит первым
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if m[row][c] is None:
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        upward = not upward
        col -= 2


def mask_fn(pattern: int):
    return {
        0: lambda r, c: (r + c) % 2 == 0,
        1: lambda r, c: r % 2 == 0,
        2: lambda r, c: c % 3 == 0,
        3: lambda r, c: (r + c) % 3 == 0,
        4: lambda r, c: (r // 2 + c // 3) % 2 == 0,
        5: lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        6: lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        7: lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    }[pattern]


def apply_mask(m, res, pattern: int):
    fn = mask_fn(pattern)
    out = [row[:] for row in m]
    for r in range(len(m)):
        for c in range(len(m)):
            if not res[r][c] and fn(r, c):
                out[r][c] ^= 1
    return out


def penalty(m) -> int:
    size = len(m)
    score = 0
    # правило 1: ряды одинаковых модулей
    for line in list(m) + [list(col) for col in zip(*m)]:
        run, prev = 1, line[0]
        for v in line[1:]:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            score += 3 + (run - 5)
    # правило 2: блоки 2x2
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    # правило 3: похоже на шаблон поиска
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for line in list(m) + [list(col) for col in zip(*m)]:
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == pat1 or seg == pat2:
                score += 40
    # правило 4: баланс тёмных и светлых
    dark = sum(sum(row) for row in m)
    total = size * size
    ratio = dark * 100 // total
    score += 10 * (abs(ratio - 50) // 5)
    return score


def format_bits(ecc: str, pattern: int) -> int:
    data = (ECC_LEVELS[ecc] << 3) | pattern
    rem = data << 10
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412


def place_format(m, ecc: str, pattern: int) -> None:
    size = len(m)
    bits = format_bits(ecc, pattern)
    for i in range(6):
        m[8][i] = (bits >> i) & 1
    m[8][7] = (bits >> 6) & 1
    m[8][8] = (bits >> 7) & 1
    m[7][8] = (bits >> 8) & 1
    for i in range(9, 15):
        m[14 - i][8] = (bits >> i) & 1
    for i in range(8):
        m[size - 1 - i][8] = (bits >> i) & 1
    for i in range(8, 15):
        m[8][size - 15 + i] = (bits >> i) & 1
    m[size - 8][8] = 1


def make_qr(text: str, ecc: str = "M", force_mask: int | None = None):
    payload = text.encode("utf-8")
    version = pick_version(len(payload), ecc)
    data = encode_data(payload, version, ecc)
    codewords = interleave(data, version, ecc)

    # все кодовые слова обязаны уместиться в модули данных
    res = reserved_mask(version)
    n_free = sum(1 for r in range(len(res)) for c in range(len(res)) if not res[r][c])
    if len(codewords) * 8 > n_free:
        raise ValueError(f"внутренняя ошибка: {len(codewords)} кодовых слов не влезают в {n_free} модулей")

    m = new_matrix(17 + 4 * version)
    place_function_patterns(m, version)
    place_data(m, codewords)

    if force_mask is not None:
        cand = apply_mask(m, res, force_mask)
        place_format(cand, ecc, force_mask)
        return cand, version, force_mask

    best, best_score, best_pat = None, None, 0
    for pat in range(8):
        cand = apply_mask(m, res, pat)
        place_format(cand, ecc, pat)
        s = penalty(cand)
        if best_score is None or s < best_score:
            best, best_score, best_pat = cand, s, pat
    return best, version, best_pat


# ---------------------------------------------------------------- вывод
def save_png(matrix, path: str, scale: int = 12, border: int = 4) -> None:
    n = len(matrix)
    side = (n + border * 2) * scale
    img = Image.new("RGB", (side, side), "white")
    px = img.load()
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                for dr in range(scale):
                    for dc in range(scale):
                        px[(c + border) * scale + dc, (r + border) * scale + dr] = (0, 0, 0)
    img.save(path)


def print_ascii(matrix, border: int = 2) -> None:
    n = len(matrix)
    pad = [[0] * (n + border * 2) for _ in range(border)]
    body = [[0] * border + row + [0] * border for row in matrix]
    grid = pad + body + pad
    for r in range(0, len(grid) - 1, 2):
        line = ""
        for c in range(len(grid[0])):
            top = grid[r][c]
            bot = grid[r + 1][c]
            line += "█" if top and bot else ("▀" if top else ("▄" if bot else " "))
        print(line)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    url = sys.argv[1]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr.png")
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]

    matrix, version, pattern = make_qr(url)
    save_png(matrix, out)
    print(f"ссылка: {url}")
    print(f"QR сохранён: {out}  (версия {version}, маска {pattern}, {len(matrix)}x{len(matrix)} модулей)")
    print("Проверьте телефоном — он должен открыть эту же ссылку.\n")
    print_ascii(matrix)


if __name__ == "__main__":
    main()
