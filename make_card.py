#!/usr/bin/env python3
"""Render 1200x1200 chart cards for @SiegeGrell posts on X.

Each card is drawn as an engineering drawing sheet: a zoned border, a drafting
grid, a title block that holds the source, and a QC inspection stamp for
verdicts. Type is Barlow, a DIN-style grotesk like the lettering on machines.

    python3 -I make_card.py SPEC.json [--outdir cards] [--replace]

SPEC.json describes one card (see README.md). Use "-" to read it from stdin.
Prints one JSON line: {"file", "raw_url", "alt", "width", "height", "bytes"}.
Exits with code 2 and a message saying what to fix when the spec is invalid
or a field is too long to fit. Text is never cut silently.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
REPO = "Jikjii/siegegrell-media"
BRANCH = "main"
HANDLE = "@SiegeGrell"

# Everything is drawn at 2x and scaled down, so lines and stamps stay smooth.
S = 2
OUT = 1200
W = H = OUT * S


def u(v):
    """Design pixels (at 1200x1200) to render pixels."""
    return round(v * S)


OUTER = u(26)            # outer border of the sheet
INNER = u(56)            # inner border; the zone band sits between the two
X0, X1 = u(100), W - u(100)
Y0 = u(100)
TB_H = u(118)            # title block height
TB_TOP = H - INNER - TB_H
Y1 = TB_TOP - u(44)      # bottom of the content area
CONTENT_W = X1 - X0

PAPER = "#fbfcfd"
GRID_MINOR = "#e9eff4"
GRID_MAJOR = "#d5e0e9"
INK = "#16191d"
INK_2 = "#4a5562"
INK_3 = "#7d8995"
RULE = "#c4cfd9"
BAR_SOFT = "#aeb9c4"

# Verdict stamps. The word always carries the meaning; color backs it up.
VERDICTS = {
    "proven": ("Proven", "#1d7a3a"),
    "promising": ("Promising", "#b86e00"),
    "unproven": ("Unproven", "#4f5d6b"),
    "false": ("False", "#c1272d"),
}

FONT_FILES = {
    "body": "Barlow-Regular.ttf",
    "medium": "Barlow-Medium.ttf",
    "semibold": "Barlow-SemiBold.ttf",
    "head": "BarlowSemiCondensed-SemiBold.ttf",
    "headbold": "BarlowSemiCondensed-Bold.ttf",
    "value": "BarlowCondensed-Bold.ttf",
    "hero": "BarlowCondensed-ExtraBold.ttf",
}
FALLBACK_FILES = {k: ("DejaVuSans.ttf" if k in ("body", "medium") else "DejaVuSans-Bold.ttf") for k in FONT_FILES}
_fonts = {}


class SpecError(Exception):
    pass


def font(weight, size):
    key = (weight, int(round(size)))
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES[weight]), key[1])
        except OSError:
            _fonts[key] = ImageFont.truetype(FALLBACK_FILES[weight], key[1])
    return _fonts[key]


def cap(f):
    """Cap height of a font, in pixels."""
    return -f.getbbox("H", anchor="ls")[1]


def text_w(text, f):
    return f.getlength(text)


def tracked_w(text, f, tracking):
    return sum(f.getlength(c) for c in text) + tracking * max(len(text) - 1, 0)


def draw_tracked(d, x, y, text, f, fill, tracking):
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill, anchor="ls")
        x += f.getlength(ch) + tracking
    return x


NUM = re.compile(r"[$~]?\d[\d,.]*[%+]?")


def tokens(text):
    """Words, with number ranges such as "700 to 800" kept on one line."""
    words, out, i = text.split(), [], 0
    while i < len(words):
        if i + 2 < len(words) and NUM.fullmatch(words[i]) and words[i + 1] in ("to", "or", "-", "–") \
                and NUM.match(words[i + 2]):
            out.append(" ".join(words[i:i + 3]))
            i += 3
        else:
            out.append(words[i])
            i += 1
    return out


def wrap(text, f, max_w):
    """Greedy word wrap. Returns None if a single word is wider than max_w."""
    lines, cur = [], ""
    for word in tokens(text):
        if text_w(word, f) > max_w:
            return None
        trial = f"{cur} {word}".strip()
        if not cur or text_w(trial, f) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def balance(text, f, max_w, lines):
    """Same number of lines, but as even as possible, so no word is left alone."""
    if len(lines) < 2:
        return lines
    lo, hi, best = max(text_w(w, f) for w in tokens(text)), max_w, lines
    while hi - lo > S:
        mid = (lo + hi) / 2
        trial = wrap(text, f, mid)
        if trial is not None and len(trial) <= len(lines):
            best, hi = trial, mid
        else:
            lo = mid
    return best


def fit(text, weight, max_w, max_lines, size, min_size):
    """Largest font size (down to min_size) at which text fits; None if it never does."""
    s = size
    while s >= min_size:
        f = font(weight, s)
        lines = wrap(text, f, max_w)
        if lines is not None and len(lines) <= max_lines:
            return f, balance(text, f, max_w, lines)
        s -= S
    return None


def text_block(text, weight, max_w, max_lines, size, min_size, lh_factor):
    got = fit(text, weight, max_w, max_lines, size, min_size)
    if not got:
        return None
    f, lines = got
    lh = round(f.size * lh_factor)
    return {"font": f, "lines": lines, "lh": lh, "cap": cap(f), "height": cap(f) + (len(lines) - 1) * lh}


def draw_block(d, b, x, top, fill):
    base = top + b["cap"]
    for i, line in enumerate(b["lines"]):
        d.text((x, base + i * b["lh"]), line, font=b["font"], fill=fill, anchor="ls")


# ---------- spec validation ----------

def clean(v):
    return " ".join(v.split())


def req_str(spec, key, max_len, where=""):
    v = spec.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SpecError(f'{where}"{key}" is required and must be a non-empty string')
    v = clean(v)
    if len(v) > max_len:
        raise SpecError(f'{where}"{key}" is {len(v)} characters; keep it to {max_len} or fewer')
    return v


def opt_str(spec, key, max_len, where=""):
    v = spec.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str):
        raise SpecError(f'{where}"{key}" must be a string')
    return req_str(spec, key, max_len, where)


def verdict_of(spec, key, required, where=""):
    v = spec.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or v.strip().lower() not in VERDICTS:
        raise SpecError(f'{where}"{key}" must be one of: {", ".join(VERDICTS)}')
    return v.strip().lower()


def validate(spec):
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a JSON object")
    kind = spec.get("type")
    if kind not in ("number", "bars", "scorecard"):
        raise SpecError('"type" must be "number", "bars" or "scorecard"')
    slug = spec.get("slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or len(slug) > 60:
        raise SpecError('"slug" must be lowercase letters, digits and single hyphens, 60 characters at most')
    date = spec.get("date")
    if date is None:
        try:
            from zoneinfo import ZoneInfo
            date = dt.datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            date = dt.date.today()
    else:
        try:
            date = dt.date.fromisoformat(date)
        except (TypeError, ValueError):
            raise SpecError('"date" must be YYYY-MM-DD')
    out = {
        "type": kind,
        "slug": slug,
        "date": date,
        "kicker": req_str(spec, "kicker", 40),
        "source": req_str(spec, "source", 160),
    }
    if kind == "number":
        out["number"] = req_str(spec, "number", 14)
        out["label"] = req_str(spec, "label", 120)
        out["note"] = opt_str(spec, "note", 170)
        out["verdict"] = verdict_of(spec, "verdict", False)
    elif kind == "bars":
        out["title"] = req_str(spec, "title", 90)
        out["subtitle"] = opt_str(spec, "subtitle", 80)
        out["note"] = opt_str(spec, "note", 150)
        bars = spec.get("bars")
        if not isinstance(bars, list) or not 2 <= len(bars) <= 8:
            raise SpecError('"bars" must be a list of 2 to 8 items')
        clean_bars = []
        for i, b in enumerate(bars):
            where = f"bars[{i}]: "
            if not isinstance(b, dict):
                raise SpecError(f"{where}each bar must be an object")
            value = b.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise SpecError(f'{where}"value" must be a number, 0 or more')
            display = opt_str(b, "display", 16, where) or f"{value:,}"
            clean_bars.append({"label": req_str(b, "label", 40, where), "value": float(value), "display": display})
        if max(b["value"] for b in clean_bars) <= 0:
            raise SpecError('at least one bar needs a "value" above 0')
        hl = spec.get("highlight")
        if hl is not None and (isinstance(hl, bool) or not isinstance(hl, int) or not 0 <= hl < len(clean_bars)):
            raise SpecError('"highlight" must be the 0-based index of one bar, or left out')
        out["bars"] = clean_bars
        out["highlight"] = hl
    else:
        out["title"] = req_str(spec, "title", 90)
        rows = spec.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 6:
            raise SpecError('"rows" must be a list of 1 to 6 items')
        out["rows"] = []
        for i, r in enumerate(rows):
            where = f"rows[{i}]: "
            if not isinstance(r, dict):
                raise SpecError(f"{where}each row must be an object")
            out["rows"].append({"claim": req_str(r, "claim", 110, where), "verdict": verdict_of(r, "verdict", True, where)})
    return out


# ---------- the sheet ----------

def date_label(d):
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def draw_sheet(d):
    """Drafting grid, double border and zone markers."""
    step = u(24)
    for major in (False, True):
        for i, x in enumerate(range(INNER, W - INNER + 1, step)):
            if (i % 5 == 0) == major:
                d.line([(x, INNER), (x, H - INNER)], fill=GRID_MAJOR if major else GRID_MINOR, width=S if major else 1)
        for i, y in enumerate(range(INNER, H - INNER + 1, step)):
            if (i % 5 == 0) == major:
                d.line([(INNER, y), (W - INNER, y)], fill=GRID_MAJOR if major else GRID_MINOR, width=S if major else 1)
    d.rectangle([OUTER, OUTER, W - OUTER, H - OUTER], outline=INK, width=u(3))
    d.rectangle([INNER, INNER, W - INNER, H - INNER], outline=INK, width=u(1.5))
    zf = font("medium", u(15))
    span = (W - 2 * INNER) / 4
    band = (OUTER + INNER) / 2
    for i in range(4):
        mid = INNER + span * (i + 0.5)
        d.text((mid, band), str(i + 1), font=zf, fill=INK_3, anchor="mm")
        d.text((mid, H - band), str(i + 1), font=zf, fill=INK_3, anchor="mm")
        d.text((band, mid), "ABCD"[i], font=zf, fill=INK_3, anchor="mm")
        d.text((W - band, mid), "ABCD"[i], font=zf, fill=INK_3, anchor="mm")
        if i:
            b = INNER + span * i
            for seg in (((b, OUTER), (b, INNER)), ((b, H - INNER), (b, H - OUTER)),
                        ((OUTER, b), (INNER, b)), ((W - INNER, b), (W - OUTER, b))):
                d.line(seg, fill=INK, width=u(1.5))


def draw_title_block(d, source, date):
    x0, x1, y0, y1 = INNER, W - INNER, TB_TOP, H - INNER
    d.rectangle([x0, y0, x1, y1], fill=PAPER)
    d.line([(x0, y0), (x1, y0)], fill=INK, width=u(1.5))
    c1 = x0 + (x1 - x0) * 0.60
    c2 = x0 + (x1 - x0) * 0.79
    for c in (c1, c2):
        d.line([(c, y0), (c, y1)], fill=INK, width=u(1.5))
    pad = u(18)
    lf = font("medium", u(16))
    label_base = y0 + u(16) + cap(lf)
    for x, label in ((x0, "Source"), (c1, "Date"), (c2, "By")):
        d.text((x + pad, label_base), label, font=lf, fill=INK_3, anchor="ls")
    top = label_base + u(16)
    got = fit(source, "semibold", c1 - x0 - 2 * pad, 2, u(25), u(18))
    if not got:
        raise SpecError('"source" is too long to fit in the title block; shorten it')
    vf, lines = got
    lh = round(vf.size * 1.22)
    for i, line in enumerate(lines):
        d.text((x0 + pad, top + cap(vf) + i * lh), line, font=vf, fill=INK, anchor="ls")
    df = font("semibold", u(25))
    d.text((c1 + pad, top + cap(df)), date_label(date), font=df, fill=INK, anchor="ls")
    hf = font("headbold", u(28))
    d.text((c2 + pad, top + cap(hf)), HANDLE, font=hf, fill=INK, anchor="ls")


def draw_kicker(d, kicker, right_limit=X1):
    f = font("head", u(30))
    if text_w(kicker, f) > right_limit - X0:
        raise SpecError('"kicker" is too long to fit; shorten it')
    base = Y0 + cap(f)
    d.text((X0, base), kicker, font=f, fill=INK_2, anchor="ls")
    return base


# ---------- the inspection stamp ----------

def seed_for(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def make_stamp(verdict, size, angle, seed):
    """Ink mask of a rotated, double-ruled rubber stamp, plus its color."""
    word, color = VERDICTS[verdict]
    word = word.upper()
    f = font("hero", size)
    tracking = size * 0.06
    tw, ch = tracked_w(word, f, tracking), cap(f)
    t_out, gap, t_in = max(2, round(size * 0.085)), round(size * 0.1), max(1, round(size * 0.035))
    pad_x, pad_y = size * 0.36, size * 0.3
    w = round(tw + 2 * (pad_x + gap + t_out))
    h = round(ch + 2 * (pad_y + gap + t_out))
    m = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(m)
    md.rounded_rectangle([0, 0, w - 1, h - 1], radius=round(size * 0.12), outline=255, width=t_out)
    o = t_out + gap
    md.rectangle([o, o, w - 1 - o, h - 1 - o], outline=255, width=t_in)
    draw_tracked(md, (w - tw) / 2, h / 2 + ch / 2, word, f, 255, tracking)
    rng = np.random.default_rng(seed)
    blot = rng.random((max(2, h // 9), max(2, w // 9)))
    blot = np.asarray(Image.fromarray((blot * 255).astype("uint8")).resize((w, h), Image.BICUBIC), dtype=np.float32) / 255
    keep = np.clip((blot - 0.05) / 0.2, 0.0, 1.0) * (rng.random((h, w)) > 0.03)
    ink = np.asarray(m, dtype=np.float32) / 255 * keep * 0.93
    mask = Image.fromarray((ink * 255).astype("uint8")).filter(ImageFilter.GaussianBlur(0.5 * S))
    return mask.rotate(angle, resample=Image.BICUBIC, expand=True), color


def apply_stamps(img, stamps):
    if not stamps:
        return img
    layer = Image.new("RGB", img.size, "white")
    for mask, color, cx, cy in stamps:
        layer.paste(Image.new("RGB", mask.size, color), (round(cx - mask.width / 2), round(cy - mask.height / 2)), mask)
    return ImageChops.multiply(img, layer)


# ---------- card types ----------

# Content starts as large as it can and shrinks until it fits with some air left over.
SCALES = [1.3, 1.22, 1.14, 1.06, 1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64, 0.58]
AIR = 0.9


def render_number(d, spec, stamps):
    head_bottom = Y0 + cap(font("head", u(30)))
    right = X1
    if spec["verdict"]:
        mask, color = make_stamp(spec["verdict"], u(58), -7, seed_for(spec["slug"], spec["verdict"]))
        cx, cy = X1 - mask.width / 2 + u(8), Y0 + mask.height / 2 - u(18)
        stamps.append((mask, color, cx, cy))
        right = cx - mask.width / 2 - u(24)
        head_bottom = max(head_bottom, cy + mask.height / 2 - u(16))
    draw_kicker(d, spec["kicker"], right)
    top = head_bottom + u(34)
    avail = Y1 - top
    for k in SCALES:
        size = min(u(420) * k, u(520))
        hf = font("hero", size)
        while text_w(spec["number"], hf) > CONTENT_W and size > u(150):
            size -= u(6)
            hf = font("hero", size)
        if text_w(spec["number"], hf) > CONTENT_W:
            raise SpecError('"number" is too wide to fit; shorten it')
        box = hf.getbbox(spec["number"], anchor="ls")
        hero_cap, hero_desc = -box[1], max(0, box[3])
        hero_gap = u(40) * k + hero_desc
        label = text_block(spec["label"], "head", CONTENT_W, 3, min(u(64) * k, u(72)), u(40), 1.08)
        note = text_block(spec["note"], "body", CONTENT_W, 3, min(u(34) * k, u(38)), u(26), 1.28) if spec["note"] else None
        if label is None or (spec["note"] and note is None):
            continue
        total = hero_cap + hero_gap + label["height"] + (u(30) * k + note["height"] if note else 0)
        if total > avail * AIR:
            continue
        y = top + (avail - total) * 0.3
        d.text((X0 - u(4), y + hero_cap), spec["number"], font=hf, fill=INK, anchor="ls")
        y += hero_cap + hero_gap
        draw_block(d, label, X0, y, INK)
        if note:
            draw_block(d, note, X0, y + label["height"] + u(30) * k, INK_2)
        return
    raise SpecError('"label" or "note" is too long to fit; shorten them')


def render_bars(d, spec, stamps):
    bars, n = spec["bars"], len(spec["bars"])
    vmax = max(b["value"] for b in bars)
    top = draw_kicker(d, spec["kicker"]) + u(40)
    avail = Y1 - top
    for k in SCALES:
        title = text_block(spec["title"], "headbold", CONTENT_W, 2, min(u(60) * k, u(68)), u(38), 1.06)
        sub = text_block(spec["subtitle"], "body", CONTENT_W, 1, min(u(32) * k, u(36)), u(24), 1.25) if spec["subtitle"] else None
        note = text_block(spec["note"], "body", CONTENT_W, 3, min(u(32) * k, u(36)), u(24), 1.28) if spec["note"] else None
        if title is None or (spec["subtitle"] and sub is None) or (spec["note"] and note is None):
            continue
        lf = font("head", u(34) * k)
        vf = font("value", u(56) * k)
        thick = round(u(54) * k)
        if any(text_w(b["label"], lf) > CONTENT_W for b in bars):
            continue
        row_h = cap(lf) + u(14) * k + thick + u(34) * k
        value_w = max(text_w(b["display"], vf) for b in bars)
        max_len = CONTENT_W - value_w - u(22)
        head_h = title["height"] + (u(18) * k + sub["height"] if sub else 0) + u(52) * k
        total = head_h + n * row_h - u(34) * k + (u(40) * k + note["height"] if note else 0)
        if total > avail * AIR or max_len < CONTENT_W * 0.4:
            continue
        y = top + (avail - total) * 0.3
        draw_block(d, title, X0, y, INK)
        y += title["height"]
        if sub:
            y += u(18) * k
            draw_block(d, sub, X0, y, INK_2)
            y += sub["height"]
        y += u(52) * k
        chart_top = y
        radius = u(3)
        for i, b in enumerate(bars):
            base = y + cap(lf)
            d.text((X0 + u(18), base), b["label"], font=lf, fill=INK, anchor="ls")
            bt = base + u(14) * k
            color = INK if spec["highlight"] is None or spec["highlight"] == i else BAR_SOFT
            length = 0 if b["value"] <= 0 else max(radius * 2, b["value"] / vmax * max_len)
            if length:
                d.rounded_rectangle([X0, bt, X0 + length, bt + thick], radius=radius, fill=color,
                                    corners=(False, True, True, False))
            d.text((X0 + length + u(16), bt + thick / 2), b["display"], font=vf, fill=INK, anchor="lm")
            y += row_h
        d.line([(X0, chart_top - u(6)), (X0, y - u(34) * k + u(6))], fill=INK, width=u(2))
        if note:
            draw_block(d, note, X0, y - u(34) * k + u(40) * k, INK_2)
        return
    raise SpecError("the title, labels or note are too long to fit; shorten them")


ROW_ANGLES = [-4, 3, -2, 4, -3, 2]


def render_scorecard(d, spec, stamps):
    rows = spec["rows"]
    top = draw_kicker(d, spec["kicker"]) + u(36)
    avail = Y1 - top
    for k in SCALES:
        title = text_block(spec["title"], "headbold", CONTENT_W, 2, min(u(58) * k, u(66)), u(38), 1.06)
        if title is None:
            continue
        marks = [make_stamp(r["verdict"], u(30) * k, ROW_ANGLES[i], seed_for(spec["slug"], i, r["verdict"]))
                 for i, r in enumerate(rows)]
        col = max(m.width for m, _ in marks)
        claim_w = CONTENT_W - col - u(30)
        blocks = [text_block(r["claim"], "medium", claim_w, 3, u(36) * k, u(26), 1.2) for r in rows]
        if any(b is None for b in blocks):
            continue
        pad = u(22) * k
        row_hs = [max(b["height"] + b["font"].size * 0.28, m.height * 0.82) + 2 * pad for b, (m, _) in zip(blocks, marks)]
        total = title["height"] + u(34) * k + sum(row_hs)
        if total > avail * AIR:
            continue
        y = top + (avail - total) * 0.3
        draw_block(d, title, X0, y, INK)
        y += title["height"] + u(34) * k
        for i, (b, (mask, color), rh) in enumerate(zip(blocks, marks, row_hs)):
            d.line([(X0, y), (X1, y)], fill=RULE if i else INK, width=u(1.5) if i else u(2))
            mid = y + rh / 2
            draw_block(d, b, X0, mid - b["height"] / 2, INK)
            stamps.append((mask, color, X1 - col / 2, mid))
            y += rh
        d.line([(X0, y), (X1, y)], fill=INK, width=u(2))
        return
    raise SpecError('the title or a "claim" is too long to fit; shorten it')


# ---------- alt text ----------

def sentence(s):
    s = s.strip()
    return s if s[-1:] in ".!?" else s + "."


def alt_text(spec):
    parts = [f"{spec['kicker']}."]
    if spec["type"] == "number":
        parts += [f"{spec['number']}:", sentence(spec["label"])]
        if spec["note"]:
            parts.append(sentence(spec["note"]))
        if spec["verdict"]:
            parts.append(f"Verdict: {VERDICTS[spec['verdict']][0]}.")
    elif spec["type"] == "bars":
        parts.append(sentence(spec["title"]))
        if spec["subtitle"]:
            parts.append(sentence(spec["subtitle"]))
        parts.append(sentence("; ".join(f"{b['label']}: {b['display']}" for b in spec["bars"])))
        if spec["note"]:
            parts.append(sentence(spec["note"]))
    else:
        parts.append(sentence(spec["title"]))
        parts += [f"{VERDICTS[r['verdict']][0]}: {sentence(r['claim'])}" for r in spec["rows"]]
    src = spec["source"]
    parts.append(sentence(src if src.lower().startswith("source") else f"Source: {src}"))
    alt = " ".join(parts)
    return alt if len(alt) <= 1000 else alt[:997].rstrip() + "..."


# ---------- main ----------

def render(spec):
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    draw_sheet(d)
    stamps = []
    {"number": render_number, "bars": render_bars, "scorecard": render_scorecard}[spec["type"]](d, spec, stamps)
    draw_title_block(d, spec["source"], spec["date"])
    img = apply_stamps(img, stamps)
    return img.resize((OUT, OUT), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser(description="Render a @SiegeGrell chart card.")
    ap.add_argument("spec", help="path to the JSON spec, or - for stdin")
    ap.add_argument("--outdir", default="cards", help="folder for the PNG, relative to this repo (default: cards)")
    ap.add_argument("--replace", action="store_true", help="overwrite a card with the same date and slug")
    args = ap.parse_args()
    try:
        raw = sys.stdin.read() if args.spec == "-" else open(args.spec, encoding="utf-8").read()
        spec = validate(json.loads(raw))
        img = render(spec)
    except (SpecError, json.JSONDecodeError, OSError) as e:
        print(f"make_card: {e}", file=sys.stderr)
        sys.exit(2)
    outdir = args.outdir if os.path.isabs(args.outdir) else os.path.join(HERE, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    stem = f"{spec['date'].isoformat()}-{spec['slug']}"
    path = os.path.join(outdir, stem + ".png")
    n = 2
    while os.path.exists(path) and not args.replace:
        path = os.path.join(outdir, f"{stem}-{n}.png")
        n += 1
    img.save(path, "PNG", optimize=True)
    rel = os.path.relpath(path, HERE).replace(os.sep, "/")
    print(json.dumps({
        "file": rel,
        "raw_url": f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{rel}",
        "alt": alt_text(spec),
        "width": OUT,
        "height": OUT,
        "bytes": os.path.getsize(path),
    }))


if __name__ == "__main__":
    main()
