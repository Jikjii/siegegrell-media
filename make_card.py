#!/usr/bin/env python3
"""Render 1600x900 chart cards for @SiegeGrell posts on X, in light or dark.

    python3 -I make_card.py SPEC.json [--outdir cards] [--replace]

SPEC.json describes one card (see README.md). Use "-" to read it from stdin.
Prints one JSON line: {"file", "raw_url", "alt", "width", "height", "bytes"}.
Exits with code 2 and a message saying what to fix when the spec is invalid
or a field is too long to fit. Text is never cut silently.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")
REPO = "Jikjii/siegegrell-media"
BRANCH = "main"
HANDLE = "@SiegeGrell"

# Everything is drawn at 2x and scaled down, so shapes are smooth.
S = 2
OUT_W, OUT_H = 1600, 900
W, H = OUT_W * S, OUT_H * S
PAD = 100 * S
HEADER_BASE = 112 * S
BODY_TOP = 178 * S
FOOTER_TOP = 776 * S
BODY_BOTTOM = FOOTER_TOP - 44 * S

# Colors: the dataviz reference palette. Light mode sits on a warm card surface;
# dark mode uses the palette's dark steps. set_theme() swaps them before drawing.
THEMES = {
    "light": {
        "SURFACE": "#f5f4ef",
        "INK": "#0b0b0b",
        "INK_2": "#52514e",
        "HAIRLINE": "#dcdbd3",
        "BASELINE": "#b9b8ae",
        "BLUE": "#2a78d6",       # categorical slot 1: the bar the story is about
        "BLUE_SOFT": "#86b6ef",  # blue step 250: the other bars
        "TINT": 0.18,            # how much verdict color goes into a pill's fill
    },
    "dark": {
        "SURFACE": "#1a1a19",
        "INK": "#f0efec",
        "INK_2": "#c3c2b7",
        "HAIRLINE": "#383835",
        "BASELINE": "#5a5954",
        "BLUE": "#3987e5",       # dark step of slot 1
        "BLUE_SOFT": "#184f95",  # blue step 600: the other bars recede on dark
        "TINT": 0.26,
    },
}
SURFACE = INK = INK_2 = HAIRLINE = BASELINE = BLUE = BLUE_SOFT = TINT = None


def set_theme(name):
    globals().update(THEMES[name])


set_theme("light")

# Status palette. The label always carries the meaning; color and icon back it up.
VERDICTS = {
    "proven": ("Proven", "#0ca30c", "#ffffff"),
    "promising": ("Promising", "#fab219", "#0b0b0b"),
    "unproven": ("Unproven", "#898781", "#ffffff"),
    "false": ("False", "#d03b3b", "#ffffff"),
}

FONT_FILES = {
    "regular": "Inter-Regular.otf",
    "medium": "Inter-Medium.otf",
    "semibold": "Inter-SemiBold.otf",
    "bold": "Inter-Bold.otf",
    "hero": "InterDisplay-SemiBold.otf",
}
FALLBACK_FILES = {
    "regular": "DejaVuSans.ttf",
    "medium": "DejaVuSans.ttf",
    "semibold": "DejaVuSans-Bold.ttf",
    "bold": "DejaVuSans-Bold.ttf",
    "hero": "DejaVuSans-Bold.ttf",
}
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


def wrap(text, f, max_w):
    """Greedy word wrap. Returns None if a single word is wider than max_w."""
    lines, cur = [], ""
    for word in text.split():
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


def fit(text, weight, max_w, max_lines, size, min_size):
    """Largest font size (down to min_size) at which text fits; None if it never does."""
    s = size
    while s >= min_size:
        f = font(weight, s)
        lines = wrap(text, f, max_w)
        if lines is not None and len(lines) <= max_lines:
            return f, lines
        s -= S
    return None


def hex_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def mix(c1, c2, t):
    a, b = hex_rgb(c1), hex_rgb(c2)
    return tuple(round(a[i] * t + b[i] * (1 - t)) for i in range(3))


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
    theme = spec.get("theme", "auto")
    if theme == "auto":
        nth = spec.get("nth", 0)
        if isinstance(nth, bool) or not isinstance(nth, int) or nth < 0:
            raise SpecError('"nth" must be 0 or more: the card\'s place among the day\'s cards, counting from 0')
        theme = ("light", "dark")[(date.toordinal() + nth) % 2]
    elif theme not in THEMES:
        raise SpecError('"theme" must be "light", "dark" or "auto"')
    out = {
        "type": kind,
        "slug": slug,
        "date": date,
        "theme": theme,
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


# ---------- shared pieces ----------

def date_label(d):
    return f"{d.strftime('%b')} {d.day}, {d.year}".upper()


def source_line(source):
    return source if source.lower().startswith("source") else f"Source: {source}"


def draw_header(d, kicker, date):
    f = font("semibold", 26 * S)
    tracking = 2.6 * S
    mark = 18 * S
    date_txt = date_label(date)
    dw = tracked_w(date_txt, f, tracking)
    kick = kicker.upper()
    room = W - 2 * PAD - mark - 18 * S - dw - 48 * S
    if tracked_w(kick, f, tracking) > room:
        raise SpecError('"kicker" is too long to fit in the header; shorten it')
    d.rounded_rectangle([PAD, HEADER_BASE - mark, PAD + mark, HEADER_BASE], radius=4 * S, fill=BLUE)
    draw_tracked(d, PAD + mark + 18 * S, HEADER_BASE, kick, f, INK, tracking)
    draw_tracked(d, W - PAD - dw, HEADER_BASE, date_txt, f, INK_2, tracking)


def draw_footer(d, source):
    d.line([(PAD, FOOTER_TOP), (W - PAD, FOOTER_TOP)], fill=HAIRLINE, width=2 * S)
    fh = font("semibold", 28 * S)
    room = W - 2 * PAD - text_w(HANDLE, fh) - 56 * S
    got = fit(source_line(source), "regular", room, 2, 28 * S, 22 * S)
    if not got:
        raise SpecError('"source" is too long to fit in two footer lines; shorten it')
    f, lines = got
    lh = round(f.size * 1.32)
    block = cap(f) + (len(lines) - 1) * lh
    base = FOOTER_TOP + (H - FOOTER_TOP - block) / 2 + cap(f)
    for i, line in enumerate(lines):
        d.text((PAD, base + i * lh), line, font=f, fill=INK_2, anchor="ls")
    d.text((W - PAD, base), HANDLE, font=fh, fill=INK, anchor="rs")


def pill_metrics(verdict, k):
    h = round(60 * S * k)
    f = font("semibold", 29 * S * k)
    icon = h - 20 * S * k
    w = 10 * S * k + icon + 14 * S * k + text_w(VERDICTS[verdict][0], f) + 26 * S * k
    return round(w), h, f, icon


def draw_icon(d, verdict, cx, cy, size, color):
    lw = max(2, round(size * 0.12))
    if verdict == "proven":
        pts = [(cx - 0.22 * size, cy + 0.01 * size), (cx - 0.06 * size, cy + 0.17 * size), (cx + 0.23 * size, cy - 0.15 * size)]
        d.line(pts, fill=color, width=lw, joint="curve")
    elif verdict == "promising":
        r = size * 0.065
        for dx in (-0.2, 0.0, 0.2):
            x = cx + dx * size
            d.ellipse([x - r, cy - r, x + r, cy + r], fill=color)
    elif verdict == "unproven":
        d.text((cx, cy + size * 0.02), "?", font=font("bold", size * 0.62), fill=color, anchor="mm")
    else:
        a = 0.17 * size
        d.line([(cx - a, cy - a), (cx + a, cy + a)], fill=color, width=lw)
        d.line([(cx - a, cy + a), (cx + a, cy - a)], fill=color, width=lw)


def draw_pill(d, x, y, verdict, k, width=None):
    label, color, glyph = VERDICTS[verdict]
    w, h, f, icon = pill_metrics(verdict, k)
    w = max(w, width or 0)
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=mix(color, SURFACE, TINT))
    cx, cy = x + 10 * S * k + icon / 2, y + h / 2
    d.ellipse([cx - icon / 2, cy - icon / 2, cx + icon / 2, cy + icon / 2], fill=color)
    draw_icon(d, verdict, cx, cy, icon, glyph)
    d.text((x + 10 * S * k + icon + 14 * S * k, cy + 1 * S), label, font=f, fill=INK, anchor="lm")
    return w, h


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


SCALES = [1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64]
CONTENT_W = W - 2 * PAD
AVAIL_H = BODY_BOTTOM - BODY_TOP


def place(total):
    """Top of a body block of the given height, sitting in the upper part of the body."""
    return BODY_TOP + max(0, (AVAIL_H - total) * 0.3)


# ---------- card types ----------

def render_number(d, spec):
    for k in SCALES:
        hero_size = 290 * S * k
        hf = font("hero", hero_size)
        while text_w(spec["number"], hf) > CONTENT_W and hero_size > 120 * S:
            hero_size -= 6 * S
            hf = font("hero", hero_size)
        if text_w(spec["number"], hf) > CONTENT_W:
            raise SpecError('"number" is too wide to fit; shorten it')
        box = hf.getbbox(spec["number"], anchor="ls")
        hero_cap, hero_desc = -box[1], max(0, box[3])
        hero_gap = 46 * S * k + hero_desc
        label = text_block(spec["label"], "semibold", CONTENT_W, 2, 58 * S * k, 40 * S, 1.17)
        note = text_block(spec["note"], "regular", CONTENT_W, 3, 36 * S * k, 26 * S, 1.33) if spec["note"] else None
        if label is None or (spec["note"] and note is None):
            continue
        total = hero_cap + hero_gap + label["height"]
        if note:
            total += 38 * S * k + note["height"]
        pill_h = pill_metrics(spec["verdict"], k)[1] if spec["verdict"] else 0
        if spec["verdict"]:
            total += 40 * S * k + pill_h
        if total > AVAIL_H:
            continue
        y = place(total)
        d.text((PAD - 6 * S * k, y + hero_cap), spec["number"], font=hf, fill=INK, anchor="ls")
        y += hero_cap + hero_gap
        draw_block(d, label, PAD, y, INK)
        y += label["height"]
        if note:
            y += 38 * S * k
            draw_block(d, note, PAD, y, INK_2)
            y += note["height"]
        if spec["verdict"]:
            y += 40 * S * k
            draw_pill(d, PAD, y, spec["verdict"], k)
        return
    raise SpecError('"label" or "note" is too long to fit; shorten them')


def render_bars(d, spec):
    bars = spec["bars"]
    n = len(bars)
    vmax = max(b["value"] for b in bars)
    for k in SCALES:
        title = text_block(spec["title"], "semibold", CONTENT_W, 2, 56 * S * k, 40 * S, 1.17)
        sub = text_block(spec["subtitle"], "regular", CONTENT_W, 1, 32 * S * k, 24 * S, 1.3) if spec["subtitle"] else None
        note = text_block(spec["note"], "regular", CONTENT_W, 2, 30 * S * k, 24 * S, 1.33) if spec["note"] else None
        if title is None or (spec["subtitle"] and sub is None) or (spec["note"] and note is None):
            continue
        row_h = round(min(86 * S * k, (AVAIL_H * 0.62) / n))
        thick = round(min(44 * S * k, row_h * 0.48))
        lf = font("medium", min(32 * S * k, row_h * 0.42))
        vf = font("semibold", min(32 * S * k, row_h * 0.42))
        label_w = max(text_w(b["label"], lf) for b in bars)
        if label_w > CONTENT_W * 0.4:
            continue
        value_w = max(text_w(b["display"], vf) for b in bars)
        base_x = PAD + label_w + 26 * S
        max_len = W - PAD - base_x - value_w - 22 * S
        if max_len < CONTENT_W * 0.3:
            continue
        chart_h = row_h * n
        total = title["height"] + (20 * S * k + sub["height"] if sub else 0) + 48 * S * k + chart_h
        if note:
            total += 34 * S * k + note["height"]
        if total > AVAIL_H:
            continue
        y = place(total)
        draw_block(d, title, PAD, y, INK)
        y += title["height"]
        if sub:
            y += 20 * S * k
            draw_block(d, sub, PAD, y, INK_2)
            y += sub["height"]
        y += 48 * S * k
        chart_top = y
        radius = round(min(9 * S, thick / 2))
        for i, b in enumerate(bars):
            cy = chart_top + i * row_h + row_h / 2
            color = BLUE if spec["highlight"] is None or spec["highlight"] == i else BLUE_SOFT
            length = 0 if b["value"] <= 0 else max(radius * 2, b["value"] / vmax * max_len)
            if length:
                d.rounded_rectangle([base_x, cy - thick / 2, base_x + length, cy + thick / 2], radius=radius,
                                    fill=color, corners=(False, True, True, False))
            d.text((base_x - 26 * S, cy), b["label"], font=lf, fill=INK, anchor="rm")
            d.text((base_x + length + 20 * S, cy), b["display"], font=vf, fill=INK, anchor="lm")
        d.line([(base_x, chart_top + row_h * 0.12), (base_x, chart_top + chart_h - row_h * 0.12)], fill=BASELINE, width=2 * S)
        y = chart_top + chart_h
        if note:
            y += 34 * S * k
            draw_block(d, note, PAD, y, INK_2)
        return
    raise SpecError("the title, labels or note are too long to fit; shorten them")


def render_scorecard(d, spec):
    rows = spec["rows"]
    for k in SCALES:
        title = text_block(spec["title"], "semibold", CONTENT_W, 2, 54 * S * k, 40 * S, 1.17)
        if title is None:
            continue
        pill_w = max(pill_metrics(r["verdict"], k)[0] for r in rows)
        pill_h = pill_metrics(rows[0]["verdict"], k)[1]
        text_x_off = pill_w + 30 * S * k
        claim_w = CONTENT_W - text_x_off
        blocks = [text_block(r["claim"], "medium", claim_w, 2, 34 * S * k, 26 * S, 1.27) for r in rows]
        if any(b is None for b in blocks):
            continue
        pad_v = 20 * S * k
        row_hs = [max(pill_h, b["height"] + b["font"].size * 0.3) + 2 * pad_v for b in blocks]
        total = title["height"] + 40 * S * k + sum(row_hs)
        if total > AVAIL_H:
            continue
        y = place(total)
        draw_block(d, title, PAD, y, INK)
        y += title["height"] + 40 * S * k
        for i, (r, b, rh) in enumerate(zip(rows, blocks, row_hs)):
            if i:
                d.line([(PAD, y), (W - PAD, y)], fill=HAIRLINE, width=2 * S)
            mid = y + rh / 2
            draw_pill(d, PAD, mid - pill_h / 2, r["verdict"], k, width=pill_w)
            draw_block(d, b, PAD + text_x_off, mid - b["height"] / 2, INK)
            y += rh
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
    parts.append(sentence(source_line(spec["source"])))
    alt = " ".join(parts)
    return alt if len(alt) <= 1000 else alt[:997].rstrip() + "..."


# ---------- main ----------

def render(spec):
    set_theme(spec["theme"])
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    draw_header(d, spec["kicker"], spec["date"])
    {"number": render_number, "bars": render_bars, "scorecard": render_scorecard}[spec["type"]](d, spec)
    draw_footer(d, spec["source"])
    return img.resize((OUT_W, OUT_H), Image.LANCZOS)


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
        "theme": spec["theme"],
        "width": OUT_W,
        "height": OUT_H,
        "bytes": os.path.getsize(path),
    }))


if __name__ == "__main__":
    main()
