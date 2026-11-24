"""Layout engine for clipper.py.

A layout is DATA (a small dict / JSON file), not code. It says what goes on the 1080x1920 canvas:
a background, then layers (the game and optionally the facecam) with a size, position, rounded
corners, border and shadow, then an optional look (colour grade) and an optional frame overlay.

Add your own: drop a .json file into the layouts folder (see layouts/my_layout.example.json).
A file with the same name as a built-in replaces it. Frames: drop 1080x1920 transparent PNGs
into layouts/frames/ and pick them by file name.

Only needs numpy (already installed) and ffmpeg.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import zlib
from pathlib import Path

import numpy as np

CW, CH = 1080, 1920


class LayoutError(Exception):
    pass


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _rgb(s: str):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# tiny PNG writer (so we do not need Pillow)
# --------------------------------------------------------------------------
def write_png(path, arr) -> None:
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    if arr.ndim == 2:
        ct, (h, w) = 0, arr.shape
    else:
        h, w, c = arr.shape
        ct = 2 if c == 3 else 6

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def _blur(a, r: int):
    """Cheap box blur x3 on a 2-D float array (approximates a gaussian)."""
    r = max(1, int(r))
    k = 2 * r + 1
    for axis in (0, 1):
        for _ in range(3):
            pad = [(r + 1, r) if i == axis else (0, 0) for i in range(2)]
            c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
            hi, lo = [slice(None)] * 2, [slice(None)] * 2
            hi[axis], lo[axis] = slice(k, None), slice(0, -k)
            a = (c[tuple(hi)] - c[tuple(lo)]) / k
    return a


def rr_alpha(w: int, h: int, r: float):
    """Anti-aliased rounded-rectangle coverage, 0..1."""
    r = max(0.0, min(float(r), w / 2.0, h / 2.0))
    y, x = np.mgrid[0:h, 0:w]
    px, py = x + 0.5, y + 0.5
    dx = np.maximum(np.maximum(r - px, px - (w - r)), 0)
    dy = np.maximum(np.maximum(r - py, py - (h - r)), 0)
    d = np.sqrt(dx * dx + dy * dy) - r
    if r == 0:
        d = np.maximum(np.maximum(np.abs(px - w / 2) - w / 2, np.abs(py - h / 2) - h / 2), -1)
    return np.clip(0.5 - d, 0, 1)


# --------------------------------------------------------------------------
# generated image assets (cached by their parameters)
# --------------------------------------------------------------------------
def _asset(cache_dir, kind: str, params: dict, maker) -> str:
    key = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12]
    p = Path(cache_dir) / f"{kind}_{key}.png"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        write_png(p, maker())
    return str(p.resolve())


def mask_png(cache_dir, w, h, r):
    return _asset(cache_dir, "mask", dict(w=w, h=h, r=r), lambda: (rr_alpha(w, h, r) * 255).astype(np.uint8))


def card_png(cache_dir, w, h, r, color, alpha=1.0):
    def mk():
        out = np.zeros((h, w, 4), np.uint8)
        out[..., :3] = _rgb(color)
        out[..., 3] = (rr_alpha(w, h, r) * alpha * 255).astype(np.uint8)
        return out
    return _asset(cache_dir, "card", dict(w=w, h=h, r=r, c=color, a=alpha), mk)


def shadow_png(cache_dir, w, h, r, pad, alpha):
    def mk():
        base = np.zeros((h + 2 * pad, w + 2 * pad))
        base[pad:pad + h, pad:pad + w] = rr_alpha(w, h, r)
        a = np.clip(_blur(base, max(2, pad // 2)) * alpha, 0, 1)
        out = np.zeros(a.shape + (4,), np.uint8)
        out[..., 3] = (a * 255).astype(np.uint8)
        return out
    return _asset(cache_dir, "shadow", dict(w=w, h=h, r=r, p=pad, a=alpha), mk)


def gradient_png(cache_dir, colors, angle=0):
    def mk():
        cols = [_rgb(c) for c in colors]
        y, x = np.mgrid[0:CH, 0:CW]
        a = math.radians(angle)
        u = (x / CW - 0.5) * math.sin(a) + (y / CH - 0.5) * math.cos(a)
        t = u / (abs(math.sin(a)) + abs(math.cos(a))) + 0.5
        stops = np.linspace(0, 1, len(cols))
        out = np.stack([np.interp(t, stops, [c[i] for c in cols]) for i in range(3)], -1)
        out += np.random.default_rng(1).uniform(-1.2, 1.2, (CH, CW, 1))  # dither: no banding
        return np.clip(out, 0, 255).astype(np.uint8)
    return _asset(cache_dir, "grad", dict(c=list(colors), a=angle), mk)


def frame_png(cache_dir, color, px=8, radius=56, inset=16, glow=0):
    def mk():
        def placed(inset_, r_):
            full = np.zeros((CH, CW))
            w, h = CW - 2 * inset_, CH - 2 * inset_
            full[inset_:inset_ + h, inset_:inset_ + w] = rr_alpha(w, h, r_)
            return full
        ring = np.clip(placed(inset, radius) - placed(inset + px, max(0, radius - px)), 0, 1)
        alpha = ring
        if glow:
            alpha = np.maximum(ring, np.clip(_blur(ring, glow) * 4, 0, 0.75))
        out = np.zeros((CH, CW, 4), np.uint8)
        out[..., :3] = _rgb(color)
        out[..., 3] = (alpha * 255).astype(np.uint8)
        return out
    return _asset(cache_dir, "frame", dict(c=color, p=px, r=radius, i=inset, g=glow), mk)


# --------------------------------------------------------------------------
# looks (colour grades, applied to the game and facecam only, never the background)
# --------------------------------------------------------------------------
LOOKS = {
    "none": "",
    "punchy": "eq=contrast=1.08:saturation=1.25:gamma=0.97",
    "cinematic": "eq=contrast=1.1:saturation=0.88:gamma=0.95,vignette=PI/5",
    "warm": "eq=saturation=1.1,colorbalance=rs=0.06:bs=-0.06:rm=0.05:bm=-0.04",
    "cool": "eq=saturation=1.05,colorbalance=rs=-0.05:bs=0.07:rm=-0.03:bm=0.05",
    "neon": "eq=contrast=1.1:saturation=1.4,colorbalance=rs=0.05:bs=0.08:rm=0.04:bm=0.07",
    "vice": "eq=contrast=1.08:saturation=1.3,colorbalance=rs=0.08:gs=-0.02:bs=0.1:rm=0.04:bm=0.06",
    "crisp": "unsharp=5:5:0.8:5:5:0.0,eq=contrast=1.05:saturation=1.1",
}

FRAMES = {
    "neon": dict(color="#ff4fa3", px=8, radius=56, inset=16, glow=14),
    "cyan": dict(color="#27e1ff", px=8, radius=56, inset=16, glow=14),
    "white": dict(color="#ffffff", px=10, radius=44, inset=14),
    "gold": dict(color="#f5c542", px=10, radius=44, inset=14, glow=6),
    "soft": dict(color="#ffffff", px=4, radius=60, inset=22),
}

# --------------------------------------------------------------------------
# built-in layouts. cx, cy = centre as a fraction of the canvas; w, h = size as a fraction.
# --------------------------------------------------------------------------
VICE = ["#12063a", "#7a1fa2", "#ff3d81", "#ffb36b"]

BUILTIN = {
    "blur": dict(
        desc="Whole game centred over a blurred copy of itself. The original look.",
        bg=dict(type="blur", blur=8), caption_y=520,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.5, w=1.0)]),
    "center": dict(
        desc="Game cropped to fill the whole screen. Biggest picture, loses the sides.",
        bg=dict(type="solid", color="#000000"), caption_y=520,
        layers=[dict(src="game", fit="cover", cx=.5, cy=.5, w=1.0, h=1.0)]),
    "stack": dict(
        desc="Facecam on top, game below, edge to edge. Needs a facecam box.",
        needs_face=True, fallback="blur", bg=dict(type="solid", color="#0b0b14"), caption_y=520,
        layers=[dict(src="face", fit="cover", cx=.5, cy=.15, w=1.0, h=.30),
                dict(src="game", fit="cover", cx=.5, cy=.65, w=1.0, h=.70)]),
    "card_stack": dict(
        desc="Two rounded cards on dark: game on top, facecam below. Needs a facecam box.",
        needs_face=True, fallback="vice_clean", bg=dict(type="solid", color="#0f1018"), caption_y=870,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.30, w=.94, round=40,
                     border=dict(px=4, color="#2b2e40"), shadow=dict(px=34, alpha=.6)),
                dict(src="face", fit="cover", cx=.5, cy=.74, w=.94, h=.30, round=40,
                     border=dict(px=4, color="#2b2e40"), shadow=dict(px=34, alpha=.6))]),
    "bubble_corner": dict(
        desc="Game fills the screen, facecam as a round bubble top right. Needs a facecam box.",
        needs_face=True, fallback="center", bg=dict(type="solid", color="#000000"), caption_y=420,
        layers=[dict(src="game", fit="cover", cx=.5, cy=.5, w=1.0, h=1.0),
                dict(src="face", shape="circle", cx=.79, cy=.12, w=.34,
                     border=dict(px=8, color="#ffffff"), shadow=dict(px=24, alpha=.5))]),
    "blur_bubble": dict(
        desc="Blur background, game centred, round facecam bubble below it. Needs a facecam box.",
        needs_face=True, fallback="blur", bg=dict(type="blur", blur=8, dim=.15), caption_y=300,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.42, w=1.0),
                dict(src="face", shape="circle", cx=.5, cy=.66, w=.32,
                     border=dict(px=8, color="#ffffff"), shadow=dict(px=24, alpha=.5))]),
    "vice_clean": dict(
        desc="Vice City sunset gradient, game in a rounded card. No facecam needed.",
        bg=dict(type="gradient", colors=VICE, angle=0), look="punchy", caption_y=430,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.45, w=.94, round=36,
                     border=dict(px=6, color="#ffffff"), shadow=dict(px=34, alpha=.55))]),
    "vice_bubble": dict(
        desc="Vice City gradient, game card, round facecam under it. Needs a facecam box.",
        needs_face=True, fallback="vice_clean", look="punchy",
        bg=dict(type="gradient", colors=VICE, angle=0), caption_y=250,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.34, w=.94, round=36,
                     border=dict(px=6, color="#ffffff"), shadow=dict(px=34, alpha=.55)),
                dict(src="face", shape="circle", cx=.5, cy=.66, w=.38,
                     border=dict(px=8, color="#ffffff"), shadow=dict(px=26, alpha=.5))]),
    "neon_frame": dict(
        desc="Dimmed blur background, game centred, glowing pink frame around the screen.",
        bg=dict(type="blur", blur=10, dim=.25), look="punchy", frame="neon", caption_y=520,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.5, w=.94, round=24)]),
    "tweet": dict(
        desc="Plain white, tweet style: your title as the tweet on top, the video in a rounded card, captions underneath.",
        bg=dict(type="solid", color="#ffffff"), caption_y=560,
        title=dict(color="#0F1419", size=60, x=70, y=320, max_chars=34, header_color="#536471"),
        caption=dict(color="#0F1419", highlight="#1D9BF0", outline=0, shadow=0, size=78, upper=False),
        layers=[dict(src="game", fit="contain", cx=.5, cy=.47, w=.92, round=36,
                     border=dict(px=2, color="#CFD9DE"))]),
    "manrope_card": dict(
        desc="Picture background, big centred title just above a glass card that hugs the video, captions underneath. Use with --font Manrope and the card_*.png backgrounds.",
        bg=dict(type="solid", color="#000000"), caption_y=450,
        title=dict(color="#FFFFFF", size=90, x=540, y=692, max_chars=18, center=True, bottom=True, max_lines=3,
                   font="Manrope ExtraBold", bold=0),
        caption=dict(color="#FFFFFF", highlight="#FFE066", outline=0, shadow=0, size=80, upper=False),
        themes={"card_peach": dict(title="#2B1636", cap="#2B1636", hi="#7A2BD9"),
                "card_dusk": dict(title="#FFFFFF", cap="#FFFFFF", hi="#FFE066"),
                "card_black": dict(title="#F4F4F5", cap="#F4F4F5", hi="#FFE066"),
                "card_grey": dict(title="#151517", cap="#151517", hi="#6D28D9")},
        layers=[dict(src="game", fit="contain", cx=.5, cy=.532, w=.9, round=30)]),
    "split": dict(
        desc="Facecam on the top half, gameplay on the bottom half, captions on the line between them. Needs a facecam.",
        needs_face=True, fallback="center", bg=dict(type="solid", color="#000000"), caption_y=905,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="face", fit="cover", cx=.5, cy=.25, w=1.0, h=.5),
                dict(src="game", fit="cover", cx=.5, cy=.75, w=1.0, h=.5)]),
    "facecam_blur": dict(
        desc="Face-only stream: the video sits centred over a blurred copy of itself, captions lower. No split.",
        bg=dict(type="blur", blur=14, dim=.2), caption_y=330,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="game", fit="contain", cx=.5, cy=.47, w=1.0, round=28)]),
    "med_top_right": dict(
        desc="Gameplay full screen, medium rounded facecam window top right.",
        needs_face=True, fallback="center", bg=dict(type="solid", color="#000000"), caption_y=520,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="game", fit="cover", cx=.5, cy=.5, w=1.0, h=1.0),
                dict(src="face", fit="cover", cx=.7, cy=.15, w=.56, h=.17, round=28,
                     border=dict(px=5, color="#ffffff"), shadow=dict(px=24, alpha=.5))]),
    "med_top_left": dict(
        desc="Gameplay full screen, medium rounded facecam window top left.",
        needs_face=True, fallback="center", bg=dict(type="solid", color="#000000"), caption_y=520,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="game", fit="cover", cx=.5, cy=.5, w=1.0, h=1.0),
                dict(src="face", fit="cover", cx=.3, cy=.15, w=.56, h=.17, round=28,
                     border=dict(px=5, color="#ffffff"), shadow=dict(px=24, alpha=.5))]),
    "med_bottom": dict(
        desc="Gameplay on top, medium wide facecam window below, captions between.",
        needs_face=True, fallback="center", bg=dict(type="solid", color="#0b0b14"), caption_y=760,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="game", fit="cover", cx=.5, cy=.3, w=1.0, h=.6),
                dict(src="face", fit="cover", cx=.5, cy=.78, w=.9, h=.26, round=32,
                     border=dict(px=5, color="#ffffff"), shadow=dict(px=24, alpha=.5))]),
    "med_card": dict(
        desc="Blurred background, game card on top, medium facecam card below, captions under both.",
        needs_face=True, fallback="blur", bg=dict(type="blur", blur=14, dim=.35), caption_y=250,
        caption=dict(color="#FFFFFF", highlight="#FFE500", outline=7, shadow=2, size=78, upper=True),
        layers=[dict(src="game", fit="contain", cx=.5, cy=.3, w=.94, round=32,
                     border=dict(px=4, color="#ffffff"), shadow=dict(px=30, alpha=.55)),
                dict(src="face", fit="cover", cx=.5, cy=.6, w=.7, h=.24, round=32,
                     border=dict(px=4, color="#ffffff"), shadow=dict(px=30, alpha=.55))]),
    "title_card": dict(
        desc="Blurred background, small rounded facecam on top, title pill (pink or peach), full landscape gameplay below. GTAArtDeco fonts.",
        needs_face=True, strict=True, bg=dict(type="blur", blur=16, dim=.25), caption_y=380, sharpen=dict(y=730, h=1190, cas=0.7, un=0.5),
        caption=dict(color="#FFFFFF", highlight="#FFAD36", outline=7, shadow=2, size=84, upper=True,
                     font="GTAArtDeco Medium", bold=0, spacing=-1),
        title=dict(color="#1A0F14", size=56, x=540, y=548, max_chars=32, center=True, max_lines=3,
                   one_line=True, min_size=44, max_w=840,
                   font="GTAArtDeco Medium", bold=0, upper=True, spacing=-2,
                   pill=dict(color="#FFFFFF", radius=12, pad_x=30, pad_y=14)),
        layers=[dict(src="game", fit="cover", cx=.5, cy=.69, w=1.0, h=.62, avoid_face=True),
                dict(src="face", fit="cover", cx=.5, cy=.145, w=.62, h=.2, round=26,
                     shadow=dict(px=26, alpha=.55))]),
    "cinema": dict(
        desc="Black letterbox bars, cinematic grade, captions in the bottom bar.",
        bg=dict(type="solid", color="#000000"), look="cinematic", caption_y=640,
        layers=[dict(src="game", fit="contain", cx=.5, cy=.5, w=1.0)]),
}
import copy as _copy
_p = _copy.deepcopy(BUILTIN["title_card"])
_p["desc"] = "Baby pink title_card (Mizo): pink pill, deep rose title text, pink caption highlight."
_p["caption"]["highlight"] = "#FFB3D0"
_p["title"]["color"] = "#6B1F3F"
_p["title"]["pill"]["color"] = "#F8C8DC"
BUILTIN["title_card_pink"] = _p
_g = _copy.deepcopy(BUILTIN["title_card"])
_g["desc"] = "Gameplay only: same title pill, fonts and captions as title_card, gameplay centred over a blurred copy of itself. No facecam."
_g["needs_face"] = False
_g.pop("strict", None)
_g["sharpen"] = dict(y=656, h=608, cas=0.7, un=0.5)
_g["layers"] = [dict(src="game", fit="contain", cx=.5, cy=.5, w=1.0)]
BUILTIN["title_card_game"] = _g
for _n, _s in BUILTIN.items():
    _s["name"] = _n


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_layouts(layouts_dir) -> tuple[dict, list]:
    """Built-ins plus any *.json in layouts_dir. Returns (layouts, warnings)."""
    out = {k: dict(v) for k, v in BUILTIN.items()}
    warns = []
    d = Path(layouts_dir)
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            if ".example" in f.name:
                continue
            try:
                spec = json.loads(f.read_text(encoding="utf-8"))
                spec.setdefault("name", f.stem)
                if not isinstance(spec.get("layers"), list):
                    raise ValueError("needs a 'layers' list")
                out[spec["name"]] = spec
            except Exception as ex:
                warns.append(f"Could not read layout file {f.name}: {ex}")
    return out, warns


def list_frames(layouts_dir) -> list:
    names = ["none"] + list(FRAMES)
    fd = Path(layouts_dir) / "frames"
    if fd.is_dir():
        names += sorted(p.stem for p in fd.glob("*.png"))
    return names


def describe(layouts: dict) -> list:
    return [dict(name=n, desc=s.get("desc", ""), needs_face=bool(s.get("needs_face")),
                 look=s.get("look", "none"), frame=s.get("frame") or "none") for n, s in layouts.items()]


def resolve(name: str, layouts: dict, has_face: bool):
    """Pick the layout, following 'fallback' when it needs a facecam we do not have. -> (spec, note)"""
    if name not in layouts:
        raise LayoutError(f"Unknown layout '{name}'. Available: {', '.join(layouts)}")
    note, seen, cur = None, set(), name
    while layouts[cur].get("needs_face") and not has_face:
        if cur in seen:
            raise LayoutError("Layout fallback loop")
        seen.add(cur)
        if layouts[cur].get("strict"):
            raise LayoutError("STOP: facecam not found in this video. Layout '" + cur + "' never falls back to another layout. "
                              "Run this link again with the Add link switch on 'Gameplay only'.")
        fb = layouts[cur].get("fallback", "blur")
        note = f"Layout '{name}' needs a facecam box (--facecam), so I used '{fb}' instead."
        cur = fb
        if cur not in layouts:
            raise LayoutError(f"Fallback layout '{cur}' does not exist")
    return layouts[cur], note


def parse_box(spec: str, W: int, H: int):
    """'x:y:w:h' in pixels, or 0-1 fractions of the frame."""
    try:
        x, y, w, h = [float(v) for v in str(spec).split(":")]
    except Exception:
        raise LayoutError("Facecam must look like x:y:w:h, for example 0.02:0.7:0.25:0.28")
    if max(x, y, w, h) <= 1.0:
        x, y, w, h = x * W, y * H, w * W, h * H
    w, h = _even(w), _even(h)
    x, y = int(max(0, min(x, W - w))), int(max(0, min(y, H - h)))
    return x, y, w, h


def probe_size(path) -> tuple:
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "json", str(path)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    s = json.loads(p.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def _resolve_frame(val, base_dir, cache_dir):
    """-> path of a 1080x1920 RGBA png, or None."""
    if val in (None, "", "none"):
        return None
    if isinstance(val, dict):
        return frame_png(cache_dir, **val)
    if val in FRAMES:
        return frame_png(cache_dir, **FRAMES[val])
    b = Path(base_dir)
    for c in (Path(val), b / "frames" / val, b / "frames" / (val + ".png"), b / val):
        if c.is_file():
            return str(c.resolve())
    raise LayoutError(f"Unknown frame '{val}'. Built-in: {', '.join(FRAMES)}; or put a PNG in layouts/frames/")


# --------------------------------------------------------------------------
# the graph builder
# --------------------------------------------------------------------------
def _shadow_alpha(w, h, r, pad, alpha):
    base = np.zeros((h + 2 * pad, w + 2 * pad))
    base[pad:pad + h, pad:pad + w] = rr_alpha(w, h, r)
    return np.clip(_blur(base, max(2, pad // 2)) * alpha, 0, 1)


def _comp(base, rgb, alpha, x, y):
    """Alpha-composite (rgb colour tuple or HxWx3 array, alpha HxW) onto base (HxWx3 float) at x,y, clipped."""
    h, w = alpha.shape
    x0, y0, x1, y1 = max(0, x), max(0, y), min(CW, x + w), min(CH, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    a = alpha[y0 - y:y1 - y, x0 - x:x1 - x][..., None]
    src = np.array(rgb, float) if np.ndim(rgb) == 1 else rgb[y0 - y:y1 - y, x0 - x:x1 - x]
    base[y0:y1, x0:x1] = base[y0:y1, x0:x1] * (1 - a) + src * a


def image_array(path):
    """A picture as a CW x CH array (scaled to fill, centre cropped). Uses ffmpeg, so no extra packages."""
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"scale={CW}:{CH}:flags=lanczos:force_original_aspect_ratio=increase,crop={CW}:{CH}",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    if len(raw) != CW * CH * 3:
        raise LayoutError(f"Could not read the background image {path}")
    return np.frombuffer(raw, np.uint8).reshape(CH, CW, 3).astype(float)


def _solid_or_gradient(bg: dict):
    if bg.get("type") == "image":
        return image_array(bg["path"])
    if bg.get("type") == "gradient":
        cols = [_rgb(c) for c in (bg.get("colors") or VICE)]
        ang = bg.get("angle", 0)
        y, x = np.mgrid[0:CH, 0:CW]
        a = math.radians(ang)
        u = (x / CW - 0.5) * math.sin(a) + (y / CH - 0.5) * math.cos(a)
        t = u / (abs(math.sin(a)) + abs(math.cos(a))) + 0.5
        stops = np.linspace(0, 1, len(cols))
        return np.stack([np.interp(t, stops, [c[i] for c in cols]) for i in range(3)], -1)
    return np.tile(np.array(_rgb(bg.get("color", "#000000")), float), (CH, CW, 1))


def build(spec: dict, src_w: int, src_h: int, facecam, fps: int, dur: float, cache_dir,
          base_dir=".", look=None, frame=None) -> dict:
    """Return {'inputs': [[ffmpeg args], ...], 'graph': str ending in [v0], 'caption_y': int}.

    Extra inputs are numbered from 1 (input 0 is the source video).

    Speed: with a solid or gradient background the background, shadows and borders are painted ONCE
    into a single cached image, and rounded corners are done with small static 'corner caps', so the
    only per-frame work is scaling the game and facecam. Blurred backgrounds are live, so those layouts
    use a slower per-frame mask instead."""
    inputs: list = []

    def add_input(path: str) -> int:
        inputs.append(["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", path])
        return len(inputs)

    layers = [dict(l) for l in spec.get("layers", [])]
    if facecam is None:
        if spec.get("needs_face"):
            raise LayoutError(f"Layout '{spec.get('name')}' needs a facecam box")
        layers = [l for l in layers if l.get("src") != "face"]

    look_name = look if look not in (None, "") else spec.get("look", "none")
    if look_name not in LOOKS:
        raise LayoutError(f"Unknown look '{look_name}'. Available: {', '.join(LOOKS)}")
    look_f = LOOKS[look_name]

    bg = spec.get("bg") or dict(type="solid", color="#000000")
    bgtype = bg.get("type", "solid")

    # ---- pass 1: geometry of every layer
    G = []
    for L in layers:
        is_face = L.get("src") == "face"
        circle = L.get("shape") == "circle"
        fx_, fy_ = float(L.get("focus", .5)), float(L.get("focus_y", .5))
        chain, rw, rh = [], src_w, src_h
        if is_face:
            x0, y0, w0, h0 = facecam
            if circle or L.get("square"):
                sq = min(w0, h0)
                chain.append(f"crop={sq}:{sq}:{x0 + (w0 - sq) // 2}:{y0 + (h0 - sq) // 2}")
                rw = rh = sq
            else:
                chain.append(f"crop={w0}:{h0}:{x0}:{y0}")
                rw, rh = w0, h0
        else:
            z = float(L.get("zoom", 1) or 1)
            if z > 1:
                nw, nh = min(rw, _even(rw / z)), min(rh, _even(rh / z))
                chain.append(f"crop={nw}:{nh}:{int((rw - nw) * fx_)}:{int((rh - nh) * fy_)}")
                rw, rh = nw, nh
        bw = _even(float(L.get("w", 1.0)) * CW)
        if circle:
            bh = bw
        elif L.get("h") is not None:
            bh = _even(float(L["h"]) * CH)
        else:
            bh = _even(bw / (rw / rh))
        if L.get("fit", "cover" if is_face else "contain") == "contain":
            sc = min(bw / rw, bh / rh)
            ow, oh = _even(rw * sc), _even(rh * sc)
        else:  # cover: crop to the box's shape FIRST (cheap), then scale
            ow, oh = bw, bh
            tar = bw / bh
            cw_, ch_ = (min(rw, _even(rh * tar)), rh) if rw / rh > tar else (rw, min(rh, _even(rw / tar)))
            if not is_face and L.get("avoid_face") and facecam and rw > cw_:  # keep the action centred; slide away from the cam only if it would cover most of the centre
                fx0, fy0, fw0, fh0 = facecam
                cx0 = int((rw - cw_) * 0.5)
                ov = max(0, min(fx0 + fw0, cx0 + cw_) - max(fx0, cx0)) / cw_
                if ov <= 0.55:
                    fx_ = 0.5
                else:  # old behaviour: slide the crop so the facecam falls outside it
                    pad, span = 48, rw - cw_
                    if fx0 + fw0 / 2 < rw / 2:
                        fx_ = min(1.0, (fx0 + fw0 + pad) / span)
                    else:
                        fx_ = max(0.0, (fx0 - pad - cw_) / span)
            chain.append(f"crop={cw_}:{ch_}:{int((rw - cw_) * fx_)}:{int((rh - ch_) * fy_)}")
        chain.append(f"scale={ow}:{oh}:flags=lanczos")
        if look_f:
            chain.append(look_f)
        radius = min(ow, oh) // 2 if circle else int(L.get("round", 0) or 0)
        x = int(round(float(L.get("cx", .5)) * CW - ow / 2))
        y = int(round(float(L.get("cy", .5)) * CH - oh / 2))
        G.append(dict(chain=chain, ow=ow, oh=oh, x=x, y=y, radius=radius,
                      shadow=L.get("shadow"), border=L.get("border")))

    def reach(g):  # the area a layer's shadow and border touch
        pad = max(int((g["shadow"] or {}).get("px", 0)), int((g["border"] or {}).get("px", 0)))
        return g["x"] - pad, g["y"] - pad, g["x"] + g["ow"] + pad, g["y"] + g["oh"] + pad

    inside = all(g["x"] >= 0 and g["y"] >= 0 and g["x"] + g["ow"] <= CW and g["y"] + g["oh"] <= CH for g in G)
    boxes = [reach(g) for g in G]
    overlap = any(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                  for n_, a in enumerate(boxes) for b in boxes[n_ + 1:])
    bake = bgtype != "blur" and inside and not overlap

    parts: list = []
    n_src = len(G) + (1 if bgtype == "blur" else 0)
    labels = [f"s{i}" for i in range(n_src)]
    if n_src == 1:
        parts.append(f"[0:v]null[{labels[0]}]")
    elif n_src > 1:
        parts.append(f"[0:v]split={n_src}" + "".join(f"[{l}]" for l in labels))
    take = iter(labels)
    base_arr = None

    if bgtype == "blur":
        sl = next(take)
        dim = bg.get("dim", 0)
        parts.append(f"[{sl}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                     f"boxblur={int(bg.get('blur', 8))}:2,scale={CW}:{CH}"
                     + (f",eq=brightness=-{dim}" if dim else "") + "[bg]")
    elif bake:
        base_arr = _solid_or_gradient(bg)
        if bgtype == "gradient":
            base_arr += np.random.default_rng(1).uniform(-1.2, 1.2, (CH, CW, 1))
        for g in G:
            if g["shadow"]:
                pad = int(g["shadow"].get("px", 30))
                sa = _shadow_alpha(g["ow"], g["oh"], g["radius"], pad, float(g["shadow"].get("alpha", .5)))
                _comp(base_arr, (0, 0, 0), sa, g["x"] - pad, g["y"] - pad + int(g["shadow"].get("dy", pad // 3)))
            if g["border"]:
                b = int(g["border"].get("px", 6))
                _comp(base_arr, _rgb(g["border"].get("color", "#ffffff")),
                      rr_alpha(g["ow"] + 2 * b, g["oh"] + 2 * b, g["radius"] + b), g["x"] - b, g["y"] - b)
        key = dict(bg=bg, img=(os.path.getmtime(bg['path']) if bg.get('type') == 'image' else 0), g=[(g["ow"], g["oh"], g["x"], g["y"], g["radius"], g["shadow"], g["border"]) for g in G])
        k = add_input(_asset(cache_dir, "base", key, lambda: np.clip(base_arr, 0, 255).astype(np.uint8)))
        parts.append(f"[{k}:v]scale={CW}:{CH},setsar=1[bg]")
    elif bgtype == "gradient":
        k = add_input(gradient_png(cache_dir, bg.get("colors") or VICE, bg.get("angle", 0)))
        parts.append(f"[{k}:v]scale={CW}:{CH},setsar=1[bg]")
    elif bgtype == "image":
        k = add_input(str(bg["path"]))
        parts.append(f"[{k}:v]scale={CW}:{CH}:force_original_aspect_ratio=increase,crop={CW}:{CH},setsar=1[bg]")
    else:
        r, g_, b_ = _rgb(bg.get("color", "#000000"))
        parts.append(f"color=c=0x{r:02x}{g_:02x}{b_:02x}:s={CW}x{CH}:r={fps}:d={dur:.3f}[bg]")

    cur = "bg"

    def put(png: str, px_: int, py_: int, tag: str):
        nonlocal cur
        k_ = add_input(png)
        parts.append(f"[{k_}:v]format=rgba[{tag}]")
        parts.append(f"[{cur}][{tag}]overlay={px_}:{py_}:format=auto[o{tag}]")
        cur = f"o{tag}"

    for i, g in enumerate(G):
        sl = next(take)
        ow, oh, x, y, radius = g["ow"], g["oh"], g["x"], g["y"], g["radius"]
        if radius > 0 and not bake:
            g["chain"].append("format=yuva420p")
        parts.append(f"[{sl}]" + ",".join(g["chain"]) + f"[a{i}]")
        lay = f"a{i}"
        if radius > 0 and not bake:
            m = add_input(mask_png(cache_dir, ow, oh, radius))
            parts.append(f"[{m}:v]format=gray[mk{i}]")
            parts.append(f"[a{i}][mk{i}]alphamerge[l{i}]")
            lay = f"l{i}"
        if not bake:
            if g["shadow"]:
                pad = int(g["shadow"].get("px", 30))
                put(shadow_png(cache_dir, ow, oh, radius, pad, float(g["shadow"].get("alpha", .5))),
                    x - pad, y - pad + int(g["shadow"].get("dy", pad // 3)), f"sh{i}")
            if g["border"]:
                bpx = int(g["border"].get("px", 6))
                put(card_png(cache_dir, ow + 2 * bpx, oh + 2 * bpx, radius + bpx,
                             g["border"].get("color", "#ffffff")), x - bpx, y - bpx, f"bd{i}")
        parts.append(f"[{cur}][{lay}]overlay={x}:{y}:format=auto[c{i}]")
        cur = f"c{i}"
        if bake and radius > 0:  # round the corners by painting the background back over them
            def mk_cap(g=g):
                cap = np.zeros((g["oh"], g["ow"], 4), np.uint8)
                cap[..., :3] = np.clip(base_arr[g["y"]:g["y"] + g["oh"], g["x"]:g["x"] + g["ow"]], 0, 255)
                cap[..., 3] = ((1 - rr_alpha(g["ow"], g["oh"], g["radius"])) * 255).astype(np.uint8)
                return cap
            capk = add_input(_asset(cache_dir, "cap", dict(key=key, i=i), mk_cap))
            parts.append(f"[{capk}:v]format=rgba[cap{i}]")
            parts.append(f"[{cur}][cap{i}]overlay={x}:{y}:format=auto[cc{i}]")
            cur = f"cc{i}"

    logo = spec.get("logo")
    if logo and logo.get("png"):
        lp = Path(logo["png"])
        if not lp.is_file():
            lp = Path(base_dir) / logo["png"]
        if lp.is_file():
            k = add_input(str(lp.resolve()))
            lw = _even(float(logo.get("w", .2)) * CW)
            lx = int(round(float(logo.get("cx", .85)) * CW - lw / 2))
            ly = int(round(float(logo.get("cy", .06)) * CH - lw / 2))
            parts.append(f"[{k}:v]scale={lw}:-2,format=rgba[lg]")
            parts.append(f"[{cur}][lg]overlay={lx}:{ly}:format=auto[lgo]")
            cur = "lgo"

    fp = _resolve_frame(frame if frame not in (None, "") else spec.get("frame"), base_dir, cache_dir)
    if fp:
        k = add_input(fp)
        parts.append(f"[{k}:v]scale={CW}:{CH},format=rgba[fr]")
        parts.append(f"[{cur}][fr]overlay=0:0:format=auto[v0]")
    else:
        parts.append(f"[{cur}]null[v0]")
    return dict(inputs=inputs, graph=";".join(parts), caption_y=int(spec.get("caption_y", 520)))


def preview_jpg(src, t: float, spec: dict, facecam, size, cache_dir, base_dir, out_jpg,
                look=None, frame=None, guide=True) -> None:
    """Render ONE frame of a layout to a small JPEG (270x480). guide=True shows where captions land."""
    g = build(spec, size[0], size[1], facecam, 30, 1.0, cache_dir, base_dir, look, frame)
    graph = g["graph"] + ";[v0]scale=270:480"
    if guide:
        top = int((CH - g["caption_y"] - 170) * 480 / CH)
        graph += f",drawbox=x=17:y={top}:w=236:h=43:color=white@0.45:t=fill"
    graph += "[pv]"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src)]
    for a in g["inputs"]:
        cmd += a
    cmd += ["-filter_complex", graph, "-map", "[pv]", "-frames:v", "1", "-q:v", "4", str(out_jpg)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise LayoutError("\n".join(p.stderr.strip().splitlines()[-4:]))
