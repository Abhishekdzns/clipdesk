#!/usr/bin/env python3
"""
clipper.py: find the best moments in a long video using chat and audio, then
cut vertical (9:16), captioned clips. Runs fully on your own computer.
Personal use tool. Needs ffmpeg + ffprobe on PATH and numpy.

Quick start:
    python clipper.py my_stream.mp4 --chat chat.json --top 10
    python clipper.py "https://www.youtube.com/watch?v=..." --top 8
    python clipper.py my_gameplay.mp4            (audio only, no chat)

Then open  out/index.html  to review the clips.
"""
from __future__ import annotations

import sys as _sys  # Windows pipes default to cp1252: a stray character in a log line must never stop a job
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
import layouts
import brain
import av_signals
import argparse
import csv
import html
import json
import math
import re
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SR = 16000  # analysis sample rate

# Words that signal a reaction in chat (matched case-insensitive).
CHAT_HYPE = (r"kekw|lul|omegalul|lulw|pog|poggers|pogchamp|no way|wtf|holy|omg|oh my god|"
             r"lmao|lmfao|clip it|clip that|let'?s go|insane|crazy|wow|\U0001F602|\U0001F923|\U0001F480")
# Words that signal a reaction in the streamer's speech.
TEXT_HYPE = (r"no way|what the|oh my god|omg|bro\b|dude\b|let'?s go|insane|crazy|holy|wtf|"
             r"are you kidding|oh no|nooo+|yooo+|bruh|wow|clip (it|that)")


# GTA VI layer: word groups that mark a reaction worth clipping. Use --gta to add them.
GTA_WORDS = (r"\bmandy\b|\bass\b|\btits?\b|\bboobs?\b|booty|thicc?|strip ?club|lap dance|dancers?|\bcops?\b|police|busted|wanted level|"
             r"\bnpcs?\b|weird|cool dog|dog island|jet ?ski|alligator|cluckin|lookalike|"
             r"gta( ?(6|vi|six))?|rockstar|lucia|jason|vice city|leonida|trailer|extended look|netflix|"
             r"is that a streamer|trunk|stream sniping|camilo|n3on|"
             r"kiss(ing|es|ed)?|make ?out|hold(ing)? hands|date night|girlfriend|boyfriend|relationship|"
             r"thirsty|simp|she'?s (so )?(fine|hot|bad)|rizz|baddie|cheating|my (wife|girl)|"
             r"romance|love scene|sex scene|bedroom|shower|bikini|"
             r"graphics|looks real|looks (so )?real|feels like a dream|better than gta ?5|"
             r"overrated|disappointed|mid\b|downgrade|day one|pre-?order|worth it|"
             r"cop(s)?|wanted level|heist|chase|crash|explod")
GTA_TOPIC = ("GTA 6 (adult game). Prefer what pulls views: relatable funny moments, awkward or spicy scenes (Lucia, Mandy, "
             "strip club, dancers, flirting, kissing, lap dance, rejection, bedroom), epic fails, getting busted by the cops "
             "or a wanted level chase, crashes and ragdoll chaos, weird or glitchy NPCs, the Cool Dog and jet ski dog, "
             "streamer-lookalike NPCs, pedestrians filming you, a streamer's first reaction, and crazy over the top reactions. "
             "Reject dead air, plain narration, sponsor reads and anything with nothing to see.")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


PROG = {"file": None, "t": 0.0, "t0": 0.0}


def tprogress(done_s: float, total_s: float) -> None:
    """Tell Clipdesk how far the transcription is (seconds done of seconds total). Written at most every 3 seconds."""
    try:
        f = PROG["file"]
        now = time.time()
        if not f or (now - PROG["t"] < 3 and done_s < total_s):
            return
        if not PROG["t0"]:
            PROG["t0"] = now
        PROG["t"] = now
        Path(f).write_text(json.dumps({"done": round(done_s, 1), "total": round(total_s, 1), "t": now, "t0": PROG["t0"]}),
                           encoding="utf-8")
    except Exception:  # noqa: BLE001 - progress must never stop a transcription
        pass


def need(tool: str) -> None:
    if shutil.which(tool) is None:
        sys.exit(f"Missing required tool: {tool}. Install it and make sure it is on your PATH.")


def sh(cmd: list, cwd: str | None = None) -> str:
    p = subprocess.run([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace", cwd=cwd)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed:\n{tail}")
    return p.stdout


def even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def hms(t: float, sep: str = ":") -> str:
    t = max(0, int(t))
    return f"{t // 3600:02d}{sep}{(t % 3600) // 60:02d}{sep}{t % 60:02d}"


def parse_time(x: str):
    x = str(x).strip()
    try:
        return float(x)
    except ValueError:
        pass
    parts = x.replace(",", ".").split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def probe(path: Path) -> dict:
    out = sh(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path])
    j = json.loads(out)
    v = next((s for s in j["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("No video stream found in the input.")
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise RuntimeError("Could not read the video duration.")
    return dict(w=int(v["width"]), h=int(v["height"]), dur=dur,
                audio=any(s.get("codec_type") == "audio" for s in j["streams"]))


# --------------------------------------------------------------------------
# input: download, chat, transcript
# --------------------------------------------------------------------------
def yt_extra():
    """Extra yt-dlp options. Age-gated videos (GTA footage often is) need a signed-in adult account.
    Best: put the browser name in browser.txt (e.g. firefox, or firefox:profilename) and keep that browser
    signed in; the login is then read live and stays fresh. Fallback: any *cookie*.txt export next to this script."""
    here_ = Path(__file__).resolve().parent
    out = ["--js-runtimes", "deno"] if shutil.which("deno") else []
    if (here_ / "browser.txt").exists():
        b = (here_ / "browser.txt").read_text(encoding="utf-8").strip()
        if b:
            return out + ["--cookies-from-browser", b]
    ck = sorted(here_.glob("*cookie*.txt"), key=lambda q: q.stat().st_mtime, reverse=True)
    if ck:
        return out + ["--cookies", str(ck[0])]
    return out + ["--extractor-args", "youtube:player_client=tv,tv_embedded,web_embedded,default"]


def check_source(url: str) -> None:
    """Original-source rule. If creators.json sits next to this script, only videos from the channels
    listed there may be downloaded. No file = every URL is refused."""
    f = Path(__file__).resolve().parent / "creators.json"
    if not f.exists():
        sys.exit("Blocked: no creators.json yet. URL downloads only work for official channels you list there "
                 "(copy creators.example.json to creators.json and add each streamer's own channel).")
    try:
        allowed = json.loads(f.read_text(encoding="utf-8")).get("creators", [])
    except Exception as ex:
        sys.exit(f"creators.json is not valid JSON: {ex}")
    allowed = [c for c in allowed if "example" not in str(c.get("name", "")).lower()
               and "xxxx" not in str(c.get("channel_id", ""))]
    ids = {str(v).lower() for c in allowed for k, v in c.items() if k in ("channel_id", "uploader_id", "twitch_login") and v}
    r = subprocess.run(["yt-dlp", *yt_extra(), "--skip-download", "--no-warnings", "--print",
                        "%(channel_id)s|%(uploader_id)s|%(channel)s|%(uploader)s", url],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    got = [x.strip() for x in (r.stdout.strip().splitlines() or [""])[-1].split("|")]
    if r.returncode != 0 or not any(got):
        why = (r.stderr.strip().splitlines() or ["no details"])[-1][:300]
        sys.exit(f"Could not read who uploaded this video, so I will not download it. yt-dlp said: {why}")
    if not ({g.lower() for g in got[:2]} | {g.lower().lstrip("@") for g in got[:2]}) & ids:
        sys.exit(f"Blocked: this video is from '{got[2] or got[3]}' (ids: {got[0]}, {got[1]}), which is not in "
                 "creators.json. Only clip original uploads from creators who gave permission.")
    log(f"Source check passed: {got[2] or got[3]} is in creators.json")


def download(url: str, outdir: Path):
    check_source(url)
    need("yt-dlp")
    outdir.mkdir(parents=True, exist_ok=True)
    tmpl = str(outdir / "source.%(ext)s")
    log("Downloading video (this can take a while)...")
    r = subprocess.run(["yt-dlp", *yt_extra(), "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--retries", "5",
                        "--fragment-retries", "5", "--merge-output-format", "mp4", "-o", tmpl, url])
    if r.returncode != 0:
        sys.exit("Download failed (see the yt-dlp lines above). YouTube may be rate limiting: wait a minute and retry.")
    vids = [p for p in outdir.glob("source.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]
    if not vids:
        sys.exit("Download finished but no video file was found.")
    chat = None
    # YouTube live chat replay, best effort. Twitch chat needs TwitchDownloaderCLI (see README).
    subprocess.run(["yt-dlp", *yt_extra(), "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                    "-o", tmpl, url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    found = list(outdir.glob("source*.live_chat.json"))
    if found:
        chat = found[0]
        log(f"Found chat replay: {chat.name}")
    return vids[0], chat


def load_chat(path: Path):
    """Return a list of (seconds, user, text). Supports TwitchDownloader JSON,
    yt-dlp YouTube live_chat, a generic JSON list, and CSV (time,user,message)."""
    msgs = []
    suf = path.suffix.lower()
    if suf == ".csv":
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) < 3:
                    continue
                t = parse_time(row[0])
                if t is None:
                    continue  # header row
                msgs.append((t, row[1], ",".join(row[2:])))
        return msgs
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "comments" in data:  # TwitchDownloaderCLI
        for c in data["comments"]:
            try:
                cm = c.get("commenter") or {}
                user = cm.get("name") or cm.get("display_name") or str(cm.get("_id", "?"))
                msgs.append((float(c["content_offset_seconds"]), user, (c.get("message") or {}).get("body", "")))
            except (KeyError, TypeError, ValueError):
                continue
        return msgs
    if isinstance(data, list):  # generic list of dicts
        for c in data:
            if not isinstance(c, dict):
                continue
            t = parse_time(c.get("time", c.get("t", c.get("offset", ""))))
            if t is None:
                continue
            msgs.append((t, str(c.get("user", c.get("author", "?"))), str(c.get("message", c.get("text", "")))))
        return msgs
    for line in text.splitlines():  # yt-dlp live_chat is JSON lines
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = obj.get("replayChatItemAction")
        if not r:
            continue
        off = float(r.get("videoOffsetTimeMsec", 0)) / 1000.0
        for act in r.get("actions", []):
            rend = act.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
            if not rend:
                continue
            runs = rend.get("message", {}).get("runs", [])
            body = "".join(x.get("text") or (x.get("emoji", {}).get("shortcuts") or [""])[0] for x in runs)
            user = rend.get("authorName", {}).get("simpleText") or rend.get("authorExternalChannelId", "?")
            msgs.append((off, user, body))
    return msgs


def _srt_time(x: str) -> float:
    x = x.strip().split()[0].replace(",", ".")
    return parse_time(x) or 0.0


def load_transcript(path: Path):
    """Segments [{start,end,text,words:[{start,end,word}]}] from .json, .srt or .vtt."""
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    txt = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    segs = []
    for block in re.split(r"\n\s*\n", txt.strip()):
        lines = block.strip().split("\n")
        i = next((k for k, ln in enumerate(lines) if "-->" in ln), None)
        if i is None:
            continue
        a, b = [_srt_time(x) for x in lines[i].split("-->")]
        text = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
        if not text:
            continue
        ws = text.split()
        step = (b - a) / max(1, len(ws))
        words = [dict(start=a + k * step, end=a + (k + 1) * step, word=w) for k, w in enumerate(ws)]
        segs.append(dict(start=a, end=b, text=text, words=words))
    return segs


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------
def audio_db_per_second(path: Path, dur: float, cache: Path) -> np.ndarray:
    if cache.exists():
        log("Using cached audio analysis.")
        return np.load(cache)
    n = int(math.ceil(dur))
    db = np.full(n, -90.0, dtype=np.float32)
    log("Analyzing audio loudness...")
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    carry, sec, step = b"", 0, SR * 2
    while True:
        data = p.stdout.read(step * 60)
        if not data:
            break
        carry += data
        full = len(carry) // step
        if full:
            a = np.frombuffer(carry[:full * step], dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt((a.reshape(full, SR) ** 2).mean(axis=1) + 1e-12)
            vals = 20 * np.log10(rms)
            end = min(n, sec + full)
            if end > sec:
                db[sec:end] = vals[:end - sec]
            sec += full
            carry = carry[full * step:]
            if sec % 1800 < 60:
                log(f"  audio: {hms(sec)} / {hms(dur)}")
    if len(carry) >= 4 and sec < n:
        a = np.frombuffer(carry[:len(carry) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        db[sec] = 20 * np.log10(np.sqrt((a ** 2).mean() + 1e-12))
        sec += 1
    p.wait()
    if sec == 0:
        raise RuntimeError("Could not decode the audio track.")
    _t = Path(str(cache) + ".tmp.npy")
    np.save(_t, db)
    os.replace(_t, cache)
    return db


def box(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x.astype(np.float32)
    return np.convolve(x.astype(np.float32), np.ones(k, dtype=np.float32) / k, mode="same")


def rolling_z(x: np.ndarray, win: int = 300, floor_frac: float = 0.5) -> np.ndarray:
    """How far above its own recent baseline each second is. Only positive spikes kept."""
    x = x.astype(np.float64)
    n = len(x)
    if n == 0:
        return x.astype(np.float32)
    win = max(5, min(win, n))
    c = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx + win // 2 + 1, 0, n)
    cnt = hi - lo
    mean = (c[hi] - c[lo]) / cnt
    std = np.sqrt(np.maximum((c2[hi] - c2[lo]) / cnt - mean * mean, 0))
    std = np.maximum(std, max(x.std() * floor_frac, 1e-3))
    return np.clip((x - mean) / std, 0, 8).astype(np.float32)


def chat_signal(msgs, n: int, hype_re: re.Pattern) -> np.ndarray:
    uniq = np.zeros(n, dtype=np.float32)
    hype = np.zeros(n, dtype=np.float32)
    seen, seen_h = set(), set()
    for t, user, text in msgs:
        s = int(t)
        if s < 0 or s >= n:
            continue
        if (s, user) not in seen:  # one count per person per second, so one spammer is one voice
            seen.add((s, user))
            uniq[s] += 1
        if (s, user) not in seen_h and hype_re.search(text or ""):
            seen_h.add((s, user))
            hype[s] += 1
    return rolling_z(box(uniq + 0.5 * hype, 5), win=600)


def load_signal_vtt(path: Path):
    """Whole-video captions (for example YouTube auto captions) used only to find keywords.
    Auto captions repeat lines, so keep the newest line of each cue and drop the flash cues."""
    txt = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    segs, last = [], ""
    for block in re.split(r"\n\s*\n", txt.strip()):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        i = next((k for k, ln in enumerate(lines) if "-->" in ln), None)
        if i is None or i + 1 >= len(lines):
            continue
        try:
            a, b = [_srt_time(x.strip().split(" ")[0]) for x in lines[i].split("-->")]
        except Exception:
            continue
        if b - a < 0.05:
            continue
        text = re.sub(r"<[^>]+>", "", lines[-1]).strip()
        if not text or text == last:
            continue
        last = text
        segs.append(dict(start=a, end=b, text=text))
    return segs


def text_signal(segs, n: int, text_re: re.Pattern) -> np.ndarray:
    hits = np.zeros(n, dtype=np.float32)
    for seg in segs:
        text = seg.get("text", "")
        s = int((seg["start"] + seg["end"]) / 2)
        if not 0 <= s < n:
            continue
        # match on the whole segment so phrases like "no way" work, count each match once
        hits[s] += len(text_re.findall(text))
        hits[s] += 0.5 * min(text.count("!"), 3)
    # three reaction phrases within about 7 seconds is a strong signal
    return np.minimum(4.0, (2.0 / 3.0) * box(hits, 7) * 7).astype(np.float32)


# --------------------------------------------------------------------------
# candidate selection
# --------------------------------------------------------------------------
def combine(sigs: dict, weights: dict) -> np.ndarray:
    names = list(sigs)
    tot = sum(weights[k] for k in names)
    return box(sum(weights[k] * sigs[k] for k in names) / tot, 5)


def score100(x: float) -> int:
    """Blended score (in standard deviations above normal) as an easy 0-100 number."""
    return int(round(100 * (1 - math.exp(-max(0.0, x) / 2.0))))


def find_candidates(sigs: dict, weights: dict, a, picks: list | None = None) -> list:
    names = list(sigs)
    comb = combine(sigs, weights)
    required = max(1, min(a.agree, len(names)))
    picks = list(picks or [])
    if len(picks) >= a.top:
        return picks
    for i in np.argsort(-comb):
        if comb[i] < a.min_score:
            break
        t = int(i)
        if t < a.skip_start:
            continue
        if any(abs(t - p["t"]) < a.min_gap for p in picks):
            continue
        lo, hi = max(0, t - 6), min(len(comb), t + 7)
        grp = {k: float(sigs[k][lo:hi].max()) for k in names}
        active = [k for k, v in grp.items() if v >= a.agree_z]
        if len(active) < required:
            continue
        picks.append(dict(t=t, score=float(comb[i]), groups=grp, active=active))
        if len(picks) >= a.top:
            break
    return picks


def window(t: float, a, dur: float, comb=None, peak: float = 0.0):
    s, e = max(0.0, t - a.pre), min(dur, t + a.post)
    if comb is not None and not a.no_extend:
        # keep the clip open while the moment is still going (score stays above a sustain level)
        sustain = max(0.5, 0.45 * peak)
        i, quiet = int(e), 0
        while i < len(comb) and i - (t - a.pre) < a.max_len and quiet < 3:
            quiet = 0 if comb[i] >= sustain else quiet + 1
            i += 1
        e = min(dur, max(e, i - quiet))
    if e - s < a.min_len:
        e = min(dur, s + a.min_len)
        s = max(0.0, e - a.min_len)
    if e - s > a.max_len:
        s = e - a.max_len
    return s, e


def snap(s: float, e: float, segs: list, a, dur: float, reach: float = 8.0):
    """Move the cut to sentence boundaries so clips do not start or end mid-sentence."""
    starts = [x["start"] for x in segs if s - reach <= x["start"] <= s]
    ends = [x["end"] for x in segs if e <= x["end"] <= e + reach]
    ns = max(starts) if starts else s
    ne = min(ends) if ends else e
    ns, ne = max(0.0, ns - 0.3), min(dur, ne + 0.8)
    return (ns, ne) if (ne - ns) <= a.max_len + 5 else (s, e)


# --------------------------------------------------------------------------
# speech to text (only for the chosen clips, so it stays fast)
# --------------------------------------------------------------------------
def add_nvidia_dlls() -> None:
    """If NVIDIA runtime libraries were installed with pip (nvidia-cublas-cu12, nvidia-cudnn-cu12),
    make Windows find their DLLs. Harmless when they are not installed."""
    try:
        import site
        roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    except Exception:
        roots = []
    for r in roots:
        base = Path(r) / "nvidia"
        if not base.is_dir():
            continue
        for b in base.glob("*/bin"):
            try:
                os.add_dll_directory(str(b))
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")


def gpu_info() -> str:
    """One line about the NVIDIA GPU, or a note that none was found."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version",
                            "--format=csv,noheader"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        line = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
        return f"GPU: {line}  (name, total, used, busy, driver)" if line else "GPU: nvidia-smi found no NVIDIA GPU"
    except Exception:
        return "GPU: nvidia-smi not available"


HINGLISH_PROMPT = ("Haan bhai, yeh kya ho raha hai? Chalo let's go, GTA 6 ka trailer dekha kya? "
                   "Bhai PS5 jeeta hai, subscribe karo aur chat mein batao. Arre yaar, kya baat hai!")


def ocr_names(src, start: float, dur: float) -> list:
    """Words that show on screen (contestant names, usernames): read a few frames with offline OCR.
    Returns [] if rapidocr is not installed or anything fails."""
    try:
        import re, tempfile, collections
        from rapidocr_onnxruntime import RapidOCR
        tmp = Path(tempfile.mkdtemp(prefix="ocr_"))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", f"{max(dur, 1):.2f}", "-i", str(src),
                        "-vf", "fps=1/4,scale=1280:-2", str(tmp / "f_%03d.jpg")], check=False, timeout=120)
        ocr = RapidOCR()
        seen = collections.Counter()
        for f in sorted(tmp.glob("f_*.jpg"))[:15]:
            res, _ = ocr(str(f))
            toks = set()
            for _b, txt, sc in (res or []):
                if float(sc) < 0.6:
                    continue
                for t in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", txt):
                    if t[0].isupper() and not t.isupper():
                        toks.add(t)
            seen.update(toks)
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()
        OCR_ERR[0] = "seen: " + ", ".join(f"{w}x{n}" for w, n in seen.most_common(40))
        return [w for w, n in seen.most_common(40) if not w.lower().startswith(("youtube", "subscribe", "members", "follow"))][:30]
    except Exception as ex:
        OCR_ERR[0] = f"{type(ex).__name__}: {str(ex)[:150]}"
        return []


EN_IN_DEVA = {"ब्रैंड": "brand", "ब्रांड": "brand", "न्यू": "new", "निउ": "new", "विद": "with", "जीटीए": "GTA", "जीटीएस": "GTA",
              "जीटियस": "GTA", "जीटीआ": "GTA", "पीएस": "PS", "सब्सक्राइब": "subscribe", "लाइक": "like", "चैट": "chat",
              "क्लिप": "clip", "लाइव": "live", "गेम": "game", "ट्रेलर": "trailer", "स्ट्रीम": "stream", "ओके": "ok",
              "हेलो": "hello", "प्लीज": "please", "थैंक्स": "thanks", "सॉरी": "sorry", "वाओ": "wow"}
OCR_ERR = [""]


_DV_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
            "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh",
            "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v",
            "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l"}
_DV_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "rh", "फ": "f"}
_DV_VOW = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
           "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_DV_MAT = {"ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
           "ौ": "au", "ॉ": "o", "ॅ": "e"}
# how people actually type these words in Hinglish (left: what the letter-by-letter spelling gives)
HINGLISH = {"theek": "theek", "theeka": "theek", "nahee": "nahi", "naheen": "nahi", "nheen": "nahi", "nahin": "nahi",
            "naheen.": "nahi.", "hain": "hain", "main": "main", "mein": "mein", "hee": "hi", "bhee": "bhi",
            "kyon": "kyun", "kyoon": "kyun", "abhee": "abhi", "sabhee": "sabhi", "kabhee": "kabhi", "jaldee": "jaldi",
            "lekin": "lekin", "aap": "aap", "aao": "aao", "chalo": "chalo", "yaar": "yaar", "bhaee": "bhai",
            "bhai": "bhai", "kee": "ki", "jee": "ji", "mujhe": "mujhe", "tujhe": "tujhe", "kyaa": "kya",
            "hoon": "hoon", "hun": "hoon", "jeette": "jeete", "jeetta": "jeeta", "jitte": "jeete", "hoo": "hu", "gaee": "gayi", "gayee": "gayi", "naa": "na",
            "peeees": "PS", "ok": "ok", "oke": "ok", "okke": "ok", "okay": "okay"}
EN_IN_DEVA = {"edres": "address", "edress": "address", "edrais": "address", "veediyo": "video", "vidiyo": "video",
              "veedeeyo": "video", "gem": "game", "gaim": "game", "chait": "chat", "chaint": "chat", "klip": "clip",
              "laiv": "live", "laaiv": "live", "trelar": "trailer", "treler": "trailer", "streem": "stream",
              "sabaskraib": "subscribe", "sabskraib": "subscribe", "laik": "like", "thainks": "thanks",
              "thainkyoo": "thank you", "soree": "sorry", "pleej": "please", "pleez": "please", "helo": "hello",
              "brand": "brand", "braind": "brand", "brained": "brand", "nyoo": "new", "niu": "new", "vid": "with",
              "vidh": "with", "jeeteeas": "GTA", "jeeteee": "GTA", "jeeteeye": "GTA", "jeeteeaye": "GTA", "jeeteeay": "GTA", "jeeteeya": "GTA",
              "jeeteeyas": "GTA", "jeeteees": "GTA", "jeeteeai": "GTA", "pees": "PS", "peeesa": "PS", "peees": "PS",
              "polis": "police", "gan": "gun", "mishan": "mission", "leval": "level", "lebal": "level",
              "baink": "bank", "manee": "money", "plen": "plane", "baik": "bike", "kaar": "car", "seetee": "city",
              "phon": "phone", "fon": "phone", "lobee": "lobby", "sarvar": "server", "pleyar": "player",
              "pleear": "player", "plear": "player", "kantestent": "contestant", "vinar": "winner", "vinr": "winner",
              "giveave": "giveaway", "givave": "giveaway", "giv": "give", "chainal": "channel", "chainl": "channel",
              "membar": "member", "membars": "members", "dons": "dance", "spin": "spin", "vheel": "wheel",
              "gift": "gift", "prize": "prize", "praij": "prize", "rivard": "reward", "taim": "time",
              "teem": "team", "geming": "gaming", "gaming": "gaming", "sar": "sir", "mem": "ma'am",
              "fain": "fine", "kool": "cool", "vah": "wah", "vaah": "wah", "vov": "wow", "vau": "wow", "bro": "bro"}


def deva_to_roman(t: str) -> str:
    """Letter-by-letter Hindi to Roman with the silent 'a' dropped the way Hindi is spoken (par, vijay, samajhna)."""
    units = []  # [roman consonant or None, vowel or 'A' (inherent) or '' (none)]
    i, n = 0, len(t)
    while i < n:
        ch = t[i]
        if ch in _DV_CONS:
            c = _DV_CONS[ch]
            if i + 1 < n and t[i + 1] == "़":
                c = _DV_NUKTA.get(ch, c)
                i += 1
            units.append([c, "A"])
        elif ch in _DV_MAT and units and units[-1][0] is not None:
            units[-1][1] = _DV_MAT[ch]
        elif ch == "्" and units:
            units[-1][1] = ""
        elif ch in _DV_VOW:
            units.append([None, _DV_VOW[ch]])
        elif ch in "ंँ" and units:  # anusvara, chandrabindu
            units.append(["N", ""])
        elif ch == "ः":
            units.append(["h", ""])
        elif "०" <= ch <= "९":
            units.append([str(ord(ch) - 0x966), ""])
        elif ch == "़":
            pass
        else:
            units.append([ch, ""])
        i += 1
    real = [k for k, u in enumerate(units) if u[0] != "N"]
    if len(real) > 1 and units[real[-1]][0] is not None and units[real[-1]][1] == "A":
        units[real[-1]][1] = ""
    has_v = lambda u: bool(u[1]) or u[0] is None
    for k in range(len(units) - 2, 0, -1):
        u, p, q = units[k], units[k - 1], units[k + 1]
        if u[0] not in (None, "N") and u[1] == "A" and has_v(p) and q[0] not in (None, "N") and has_v(q):
            u[1] = ""
    short = len(units) <= 3
    for k, u in enumerate(units):  # long vowels in a short closed word: baat, yaar, saath, jeet
        if u[1] in ("a", "i", "u") and u[0] is not None:
            pass
        if short and u[0] is not None and k + 1 < len(units) and units[k + 1][0] not in (None, "N") and units[k + 1][1] == "":
            u[1] = {"a": "aa"}.get(u[1], u[1]) if u[1] == "a" and t.count("ा") else u[1]
        if u[1] == "ee" and k == len(units) - 1:
            u[1] = "i"
    out = ""
    for k, (c, v) in enumerate(units):
        if c == "N":
            nxt = units[k + 1][0] if k + 1 < len(units) else ""
            out += "m" if nxt and nxt[:1] in "pbm" else "n"
            continue
        out += (c or "") + ("a" if v == "A" else v)
    return out


def romanize_words(words: list) -> list:
    """Hindi words come out in Devanagari. Show them as Hinglish in Roman letters, English words stay English.
    Anything in another script (a speech model hallucination like Japanese, or a lone symbol) is dropped."""
    import re
    keep = []
    for w in words:
        t = re.sub(r"[^\u0900-\u097F\u0020-\u007E\u00C0-\u024F$€£₹%]", "", w["word"]).replace("\u0950", "")
        if not re.search(r"[A-Za-z0-9\u0900-\u0963\u0966-\u097F]", t):
            continue
        w["word"] = t
        keep.append(w)
    words[:] = keep
    for w in words:
        t = w["word"]
        if not re.search("[ऀ-ॿ]", t):
            core = t.strip(".,!?;:\"'")
            fix = HINGLISH.get(core.lower())
            if core and fix and fix != core.lower():
                w["word"] = t.replace(core, fix if core[0].islower() else fix[:1].upper() + fix[1:])
            continue
        lead = t[:len(t) - len(t.lstrip("\"'(¿¡"))]
        tail = t[len(t.rstrip(".,!?;:\"')।॥")):]
        core = t[len(lead):len(t) - len(tail)] if tail else t[len(lead):]
        tail = tail.replace("।", ".").replace("॥", ".")
        x = deva_to_roman(core).lower()
        x = EN_IN_DEVA.get(x) or HINGLISH.get(x) or x
        w["word"] = (lead + x + tail) if x else t
    return words


def snap_names(words: list, names: list) -> list:
    """If a spoken word is almost a name seen on screen (Arita vs Anita), use the on-screen spelling."""
    import difflib
    import re
    parts = []
    for n in names:
        parts += re.findall(r"[A-Z][a-z]{3,}", n) or [n]
    low = {n.lower(): n for n in parts}
    for w in words:
        core = w["word"].strip(".,!?\"'")
        if len(core) < 4 or not core.isascii():
            continue
        if core.lower() in low:
            w["word"] = w["word"].replace(core, low[core.lower()])
            continue
        m = difflib.get_close_matches(core.lower(), list(low), n=1, cutoff=0.8)
        if m and abs(len(m[0]) - len(core)) <= 1:
            w["word"] = w["word"].replace(core, low[m[0]])
            continue
        sk = re.sub(r"[aeiouy]+", "", core.lower())
        hit = [n for n in low if len(sk) >= 2 and n[0] == core[0].lower() and re.sub(r"[aeiouy]+", "", n) == sk]
        if len(hit) == 1:
            w["word"] = w["word"].replace(core, low[hit[0]])
    return words


DEFAULT_VOCAB = "GTA 6, GTA VI, Grand Theft Auto 6, Vice City, Leonida, Lucia, Jason, Rockstar, PS5, trailer."


def fix_words(words: list) -> list:
    """Small spelling fixes for game terms the speech models often get wrong."""
    for i, w in enumerate(words):
        t = w["word"].strip(".,!?").lower()
        nxt = words[i + 1]["word"].strip(".,!?").lower() if i + 1 < len(words) else ""
        if t in ("gts", "gta", "gt", "gtr", "gtv") and nxt in ("6", "six", "vi"):
            w["word"] = w["word"].replace(w["word"].strip(".,!?"), "GTA")
        elif t in ("gta6", "gts6", "gta-6"):
            w["word"] = w["word"].replace(w["word"].strip(".,!?"), "GTA 6")
    return words


def collapse_repeats(words: list, keep: int = 3) -> list:
    """Whisper loops ("wet wet wet wet"). Keep at most `keep` identical words in a row."""
    out, run = [], 0
    for w in words:
        key = re.sub(r"\W", "", w["word"]).lower()
        if out and key and key == re.sub(r"\W", "", out[-1]["word"]).lower():
            run += 1
            if run >= keep:
                continue
        else:
            run = 0
        out.append(w)
    return out


def tidy_words(words: list, max_len: float = 0.8, min_len: float = 0.12) -> list:
    """Time every word on its own: in order, never stretched over silence, never longer than the gap to the next."""
    words = sorted(words, key=lambda w: w["start"])
    for i, w in enumerate(words):
        nxt = words[i + 1]["start"] if i + 1 < len(words) else None
        end = min(w["end"], w["start"] + max_len)
        if nxt is not None and nxt > w["start"]:
            end = min(end, nxt)
        w["end"] = max(end, w["start"] + min_len) if nxt is None or nxt - w["start"] >= min_len else end
    return words


class Whisper:
    def __init__(self, model: str, device: str, vocab: str):
        add_nvidia_dlls()
        log(gpu_info())
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            sys.exit("Captions need faster-whisper:  pip install faster-whisper   (or run with --no-captions)")
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        if model == "auto":
            model = "large-v3" if device == "cuda" else "small"
        self.cls, self.model, self.vocab = WhisperModel, model, vocab or ("" if os.environ.get("CLIPPER_LANG") == "hi" else DEFAULT_VOCAB)
        self.device, self.m, self.pk = device, None, None
        lang = os.environ.get("CLIPPER_LANG", "")
        log(f"Parakeet check: language={lang!r}, engine={os.environ.get('CLIPPER_ENGINE', 'parakeet')!r}")
        if lang in ("", "en") and os.environ.get("CLIPPER_ENGINE", "parakeet") != "whisper":
            try:  # English: Parakeet is cleaner, punctuated and faster. Hindi/Hinglish stays on Whisper.
                import onnx_asr
                log("Loading Parakeet TDT 0.6B v2 for English captions...")
                self.pk = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2")
            except Exception as ex:
                log(f"  Parakeet not available ({str(ex)[:80]}), using Whisper")
        if self.pk is None:
            log(f"Loading Whisper model '{model}' on {device} (first run downloads it)...")
            self.load(device)

    def run_parakeet(self, wav: Path, offset: float) -> list:
        import tempfile, wave
        import numpy as np
        # Read the wav piece by piece (a 24 hour stream is 5+ GB as floats, which can exhaust RAM and force the slow fallback)
        wf = wave.open(str(wav), "rb")
        total = wf.getnframes()
        # same cutting as the N3ON $350 benchmark: equal parts of at most 40 s
        n_parts = max(1, int(np.ceil(total / (40.0 * SR))))
        step = int(np.ceil(total / n_parts))
        out = []
        for p0 in range(0, max(total, 1), step):
            part = np.frombuffer(wf.readframes(step), dtype=np.int16).astype(np.float32) / 32768.0
            if len(part) < SR // 5:
                continue
            if len(part) < SR:
                part = np.concatenate([part, np.zeros(SR - len(part), dtype=part.dtype)])
            base = offset + p0 / SR
            tprogress(p0 / SR, total / SR)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                tmp = tf.name
            try:
                with wave.open(tmp, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
                    w.writeframes((np.clip(part, -1, 1) * 32767).astype(np.int16).tobytes())
                r = self.pk.with_timestamps().recognize(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            words = []
            for tok, ts in zip(r.tokens, r.timestamps):
                if not tok.strip():
                    continue
                if tok[:1] in (" ", "$") or not words or (tok[:1].isdigit() and words[-1]["word"][-1:].isalpha()) or (tok[:1].isalpha() and words[-1]["word"][-1:].isdigit()):
                    words.append(dict(start=base + float(ts), end=base + float(ts) + 0.3, word=tok.strip()))
                else:
                    words[-1]["word"] += tok
                    words[-1]["end"] = base + float(ts) + 0.3
            for i in range(len(words) - 1):  # a word ends no later than the next one starts
                if words[i + 1]["start"] > words[i]["start"]:
                    words[i]["end"] = min(words[i]["end"], words[i + 1]["start"])
            fix_words(words)
            cur = []
            for w in words:
                cur.append(w)
                if w["word"][-1:] in ".?!" and len(cur) >= 2:
                    out.append(dict(start=cur[0]["start"], end=cur[-1]["end"], text=" ".join(x["word"] for x in cur), words=cur, eng="pk"))
                    cur = []
            if cur:
                out.append(dict(start=cur[0]["start"], end=cur[-1]["end"], text=" ".join(x["word"] for x in cur), words=cur, eng="pk"))
        wf.close()
        return out

    def hi_windows(self, audio, win=12.0):
        """Cut points for short Hindi windows, each placed on the quietest moment so no word is split."""
        n = len(audio)
        cuts, pos, fr = [0], 0, int(0.05 * SR)
        while n - pos > int((win + 4) * SR):
            lo, hi = pos + int((win - 3) * SR), pos + int((win + 3) * SR)
            seg = np.abs(audio[lo:hi])
            k = len(seg) // fr
            if k < 2:
                pos += int(win * SR); cuts.append(pos); continue
            en = seg[:k * fr].reshape(k, fr).mean(axis=1)
            en = np.convolve(en, np.ones(3) / 3, mode="same")
            pos = lo + int(np.argmin(en)) * fr + fr // 2
            cuts.append(pos)
        cuts.append(n)
        return cuts

    def run_hi(self, audio, offset):
        """Hindi/Hinglish: decode short independent windows. One long window drifts and loops after ~30 s."""
        cuts = self.hi_windows(audio, float(os.environ.get("CLIPPER_HI_WIN", "8")))
        use_vad = os.environ.get("CLIPPER_HI_VAD", "0") == "1"  # VAD drops speech under loud game/giveaway music
        words_all, segs_out = [], []
        for a0, a1 in zip(cuts, cuts[1:]):
            part = audio[a0:a1]
            if len(part) < SR // 4:
                continue
            base = offset + a0 / SR
            tprogress(a0 / SR, len(audio) / SR)

            def go():
                segs, _ = self.m.transcribe(
                    part, word_timestamps=True, condition_on_previous_text=False, language="hi", beam_size=5,
                    temperature=(0.0, 0.2, 0.4), compression_ratio_threshold=2.0, log_prob_threshold=-1.0,
                    no_speech_threshold=0.6, repetition_penalty=1.1, vad_filter=use_vad,
                    initial_prompt=os.environ.get("CLIPPER_HI_PROMPT") or None,
                    **(dict(vad_parameters=dict(threshold=0.25, min_silence_duration_ms=500, speech_pad_ms=300)) if use_vad else {}))
                return list(segs)
            try:
                segs = go()
            except Exception as ex:
                if self.device != "cuda":
                    raise
                log(f"  GPU transcription failed ({str(ex)[:80]}), switching to CPU")
                self.load("cpu")
                segs = go()
            for s in segs:
                if getattr(s, "no_speech_prob", 0) > 0.8 and getattr(s, "avg_logprob", 0) < -1.0:
                    continue
                if getattr(s, "avg_logprob", 0) < -1.6:  # garbage window text
                    continue
                ws = [dict(start=w.start + base, end=w.end + base, word=w.word.strip()) for w in (s.words or []) if w.word.strip()]
                words_all.extend(ws)
        words = tidy_words(collapse_repeats(words_all))
        words = romanize_words(fix_words(words))
        if self.names:
            words = snap_names(words, self.names)
        words = tidy_words(words)
        cur = []
        for w in words:
            cur.append(w)
            if w["word"][-1:] in ".?!," and len(cur) >= 3 or len(cur) >= 8:
                segs_out.append(dict(start=cur[0]["start"], end=cur[-1]["end"], text=" ".join(x["word"] for x in cur), words=cur))
                cur = []
        if cur:
            segs_out.append(dict(start=cur[0]["start"], end=cur[-1]["end"], text=" ".join(x["word"] for x in cur), words=cur))
        return segs_out

    def load(self, device: str) -> None:
        self.device = device
        self.m = self.cls(self.model, device=device, compute_type="float16" if device == "cuda" else "int8")

    @staticmethod
    def read_wav(wav: Path):
        # Read the audio ourselves and hand Whisper an array. This avoids the PyAV decoder,
        # whose newer versions break faster-whisper's file loading.
        import wave
        with wave.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def run(self, wav: Path, offset: float, legacy: bool | None = None) -> list:
        if self.pk is not None:
            try:
                return self.run_parakeet(wav, offset)
            except Exception as ex:
                log(f"  Parakeet error, using the slower Whisper for this one: {ex!r}"[:260])
                self.pk = None
        if self.m is None:
            self.load(self.device)
        if legacy is None:  # old settings stay the default; CLIPPER_WHISPER=new switches to the test profile
            legacy = os.environ.get("CLIPPER_WHISPER", "old") != "new" and os.environ.get("CLIPPER_LANG") != "hi"
        audio = self.read_wav(wav)
        self.names = []
        if os.environ.get("CLIPPER_LANG") == "hi" and getattr(self, "src", None):
            self.names = ocr_names(self.src, offset, len(audio) / SR)
            if self.names:
                log("  on-screen names: " + ", ".join(self.names))

        if os.environ.get("CLIPPER_LANG") == "hi":
            return self.run_hi(audio, offset)

        def go():
            if legacy:  # old settings, kept only so a test can compare
                kw = dict(vad_filter=True, beam_size=1)
            else:  # keeps speech that sits over game music and gunfire
                kw = dict(vad_filter=True, beam_size=5,
                          vad_parameters=dict(threshold=0.35, min_silence_duration_ms=700, speech_pad_ms=300))
            segs, _ = self.m.transcribe(audio, word_timestamps=True, condition_on_previous_text=False,
                                        initial_prompt=self.vocab or os.environ.get("CLIPPER_PROMPT") or None,
                                        language=os.environ.get("CLIPPER_LANG") or None, **kw)
            return list(segs)  # decoding is lazy, so force it here to catch GPU errors
        try:
            segs = go()
        except Exception as ex:
            if self.device != "cuda":
                raise
            log(f"  GPU transcription failed ({str(ex)[:80]}), switching to CPU")
            self.load("cpu")
            segs = go()
        out = []
        for s in segs:
            nsp = getattr(s, "no_speech_prob", 0)
            if (nsp > 0.6) if legacy else (nsp > 0.8 and getattr(s, "avg_logprob", 0) < -1.0):
                continue
            words = [dict(start=w.start + offset, end=w.end + offset, word=w.word.strip())
                     for w in (s.words or []) if w.word.strip()]
            out.append(dict(start=s.start + offset, end=s.end + offset, text=" ".join(x["word"] for x in words) if words else s.text.strip(), words=(snap_names(romanize_words(fix_words(words)), self.names) if self.names else romanize_words(fix_words(words)))))
        return out


def extract_wav(src: Path, s: float, e: float, wav: Path) -> bool:
    try:
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", src,
            "-vn", "-ac", "1", "-ar", str(SR), wav])
        return wav.exists() and wav.stat().st_size > 1000
    except RuntimeError:
        return False


# --------------------------------------------------------------------------
# captions and rendering
# --------------------------------------------------------------------------
def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def ass_escape(s: str) -> str:
    return s.replace("\\", " ").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ass_col(c: str) -> str:
    c = c.lstrip("#")
    return "&H00" + c[4:6] + c[2:4] + c[0:2]



TITLE_EMO = None
_EMO_MAP = [("dance","dance"),("club","dance"),("strip","dance"),("lap ","dance"),("panic","panic"),("nervous","nervous"),("beg","beg"),("worst","bad"),("awkward","bad"),
    ("plane","plane"),("crash","crash"),("shark","shark"),("basketball","ball"),("ps5","game"),("copies","game"),("gta","game"),("game","game"),("$","money"),("money","money"),("tip","money"),
    ("drake","mic"),("girl","eyes"),("chat","eyes"),("kiss","kiss"),("fire","fire"),("irl","phone"),("stream","tv"),("scream","shock"),("shock","shock"),("laugh","laugh"),
    ("dead","skull"),("die","skull"),("shoot","gun"),("gun","gun"),("cop","cop"),("police","cop"),("door","door"),("turned away","door"),("minute","clock"),("drink","drink"),
    ("love","heart"),("rage","rage"),("angry","rage"),("gift","gift"),("cry","cry"),("run","run"),("boat","boat"),("bike","bike"),("party","party"),("hot","hot"),("car","car"),("lap","dance")]


def pick_emoji(text: str):
    d = Path(__file__).resolve().parent / "emoji"
    tl = text.lower()
    for k, n in _EMO_MAP:
        hit = (k in tl) if k in ("$", "turned away") else bool(re.search(r"\b" + re.escape(k), tl))
        if hit and (d / (n + ".png")).exists():
            return d / (n + ".png")
    f = d / "wow.png"
    return f if f.exists() else None

def title_events(text: str, by: str, dur: float, t: dict) -> list:
    """The tweet text (and an optional small header line) for the whole clip, top left."""
    global TITLE_EMO
    TITLE_EMO = None
    emo = pick_emoji(text) if (t.get("emoji", True) and t.get("pill")) else None
    esz = int(int(t.get("size", 56)) * 1.05)
    egap = 16 if emo else 0
    lim = int(t.get("max_chars", 34))
    if t.get("upper"):
        text = text.upper()
    words, lines, cur = ass_escape(text).split(), [], ""
    if t.get("one_line") and t.get("center"):  # one line if it fits: shrink the size down to min_size first
        try:
            from PIL import ImageFont
            fd = Path(__file__).resolve().parent / "fonts"
            fam = str(t.get("font", "")).replace(" ", "-")
            ff = next(p for p in (fd / (fam + ".ttf"), fd / (fam + ".otf")) if p.exists())
            sp = float(t.get("spacing", 0) or 0)
            one = " ".join(words)
            for sz in range(int(t.get("size", 60)), int(t.get("min_size", 44)) - 1, -2):
                fnt = ImageFont.truetype(str(ff), sz)
                a_, d_ = fnt.getmetrics()
                if fnt.getlength(one) * sz / float(a_ + d_) + sp * len(one) <= float(t.get("max_w", 900)) - (esz + egap if emo else 0):
                    t = dict(t, size=sz, _fs=sz)
                    lim = 10 ** 6
                    break
        except Exception:  # noqa: BLE001
            pass
    for w in words:
        if cur and len(cur) + 1 + len(w) > lim:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    x, y, size = int(t.get("x", 70)), int(t.get("y", 210)), int(t.get("size", 60))
    out = []
    if t.get("center"):
        ml = int(t.get("max_lines", 3))
        if len(lines) > ml:
            lines = lines[:ml]
            lines[-1] = lines[-1].rstrip(" ,.") + "..."
        if t.get("pill"):
            pl = t["pill"]
            fam = str(t.get("font", ""))
            fw = size * 0.6
            lh = size * 1.25
            try:
                from PIL import ImageFont
                fd = Path(__file__).resolve().parent / "fonts"
                ff = next(c for c in (fd / (fam.replace(" ", "-") + ".ttf"), fd / (fam.replace(" ", "-") + ".otf"),
                                      fd / (fam.replace(" ", "-") + "-Bold.ttf"), fd / (fam.replace(" ", "-") + "-Bold.otf")) if c.exists())
                fnt = ImageFont.truetype(str(ff), size)
                asc, desc = fnt.getmetrics()
                sc = size / float(asc + desc)  # ASS font size is the line height, not the em
                lh = size
                tw = max(fnt.getlength(l.replace("\\N", " ")) for l in lines) * sc
            except Exception:  # noqa: BLE001
                tw = max(len(l) for l in lines) * fw
            px, py, rad = int(pl.get("pad_x", 34)), int(pl.get("pad_y", 22)), int(pl.get("radius", 30))
            if emo:
                esz = int(size * 1.05)
            ew = (esz + egap) if emo else 0
            w_, h_ = int(tw + 2 * px + ew), int(lh * len(lines) + 2 * py)
            rad = min(rad, h_ // 2, w_ // 2)
            k = int(rad * 0.5523)
            _pc = pl.get("color", "#FFFFFF")
            if isinstance(_pc, (list, tuple)):
                _pc = _pc[sum(map(ord, str(text))) % len(_pc)]
            col = _ass_col(_pc)[4:]
            d = (f"m {rad} 0 l {w_ - rad} 0 b {w_ - rad + k} 0 {w_} {rad - k} {w_} {rad} l {w_} {h_ - rad} "
                 f"b {w_} {h_ - rad + k} {w_ - rad + k} {h_} {w_ - rad} {h_} l {rad} {h_} "
                 f"b {rad - k} {h_} 0 {h_ - rad + k} 0 {h_ - rad} l 0 {rad} b 0 {rad - k} {rad - k} 0 {rad} 0")
            out.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\an7\\pos({x - w_ // 2},{y - py})\\p1\\1c&H{col}&\\bord0\\shad0}}{d}")
            fs = f"\\fs{t['_fs']}" if t.get("_fs") else ""
            tx = x - w_ // 2 + px + int(tw) // 2
            out.append(f"Dialogue: 2,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\an8\\pos({tx},{y}){fs}}}" + "\\N".join(lines))
            if emo:
                TITLE_EMO = dict(path=str(emo), size=esz, x=x - w_ // 2 + px + int(tw) + egap, y=int(y - py + h_ / 2 - esz / 2))
            return out
        out.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\an{2 if t.get('bottom') else 8}\\pos({x},{y})}}" + "\\N".join(lines))
        return out
    if by:
        out.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Hdr,,0,0,0,,{{\\pos({x},{y - 78})}}{ass_escape(by)}")
    out.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\pos({x},{y})}}" + "\\N".join(lines))
    return out


def build_ass(words: list, clip_start: float, path: Path, font: str, upper: bool, margin_v: int = 520,
              style: str = "karaoke", color: str = "#FFE500", cap: dict | None = None,
              title: str = "", title_by: str = "", tspec: dict | None = None, dur: float = 0.0,
              hook: str = "", hook_spec: dict | None = None) -> bool:
    cap = cap or {}
    if "color" in cap or "upper" in cap:
        upper = bool(cap.get("upper", upper))
    if not words and not title and not hook:
        return False
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or sum(len(x["word"]) + 1 for x in cur) + len(w["word"]) > 17 or w["start"] - cur[-1]["end"] > 0.7 or cur[-1]["word"][-1:] in ".?!"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    base_c = _ass_col(cap.get("color", "#FFFFFF"))
    out_c = _ass_col(cap.get("outline_color", "#000000"))
    out_w = int(cap.get("outline", 6))
    sh_w = int(cap.get("shadow", 2))
    fsize = int(cap.get("size", 84))
    ts = tspec or {}
    extra = ""
    if title:
        tsz = int(ts.get("size", 60))
        extra = (f"Style: Title,{ts.get('font', font)},{tsz},{_ass_col(ts.get('color', '#FFFFFF'))},&H000000FF,&H00000000,&H00000000,{ts.get('bold', -1)},0,0,0,100,100,{ts.get('spacing', 0)},0,1,0,0,7,0,0,0,1\n"
                 f"Style: Hdr,{font},{int(tsz * .62)},{_ass_col(ts.get('header_color', '#AAAAAA'))},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n")
    hs = hook_spec or {}
    if hook:
        extra += (f"Style: Hook,{font},{int(hs.get('size', 118))},{_ass_col(hs.get('color', '#FFE500'))},&H000000FF,"
                  f"{_ass_col(hs.get('outline_color', '#000000'))},&H96000000,-1,0,0,0,100,100,{int(hs.get('spacing', 2))},0,1,"
                  f"{int(hs.get('outline', 9))},4,5,60,60,0,1\n")
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Cap,{cap.get('font', font)},{fsize},{base_c},&H000000FF,{out_c},&H64000000,{cap.get('bold', -1)},0,0,0,100,100,{cap.get('spacing', 0)},0,1,{out_w},{sh_w},2,"
            f"70,70,{margin_v},1\n{extra}\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = title_events(title, title_by, dur, ts) if title else []
    if hook:  # big pop-in hook over the first 1.6 seconds: a question the end of the clip answers
        hw, hl, hc = ass_escape(hook).upper().split(), [], ""
        for w in hw:
            if hc and len(hc) + 1 + len(w) > int(hs.get("max_chars", 13)):
                hl.append(hc); hc = w
            else:
                hc = (hc + " " + w).strip()
        if hc:
            hl.append(hc)
        hy = int(hs.get("y", 900))
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(float(hs.get('secs', 1.6)))},Hook,,0,0,0,,"
                     f"{{\\an5\\pos(540,{hy})\\fad(60,220)\\fscx55\\fscy55\\t(0,150,\\fscx118\\fscy118)\\t(150,280,\\fscx100\\fscy100)}}"
                     + "\\N".join(hl))
    hi = "&H" + "".join(reversed([(cap.get("highlight") or color).lstrip("#")[k:k + 2] for k in (0, 2, 4)])).upper() + "&"
    reset = "&H" + "".join(reversed([cap.get("color", "#FFFFFF").lstrip("#")[k:k + 2] for k in (0, 2, 4)])).upper() + "&"
    for i, ch in enumerate(chunks):
        st = ch[0]["start"] - clip_start
        en = ch[-1]["end"] - clip_start
        if i + 1 < len(chunks):
            nxt = chunks[i + 1][0]["start"] - clip_start
            if 0 <= nxt - en < 0.3:
                en = nxt
        en = max(en, st + 0.35)
        toks = [ass_escape(w["word"]) for w in ch]
        toks = [x.upper() for x in toks] if upper else toks
        if style != "karaoke":
            lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,{' '.join(toks)}")
            continue
        for k, w in enumerate(ch):  # one event per word: the spoken word is coloured and slightly bigger
            a0 = st if k == 0 else w["start"] - clip_start
            a1 = en if k == len(ch) - 1 else ch[k + 1]["start"] - clip_start
            if a1 - a0 < 0.02:
                continue
            txt = " ".join((f"{{\\c{hi}\\fscx112\\fscy112}}{x}{{\\c{reset}\\fscx100\\fscy100}}" if q == k else x)
                           for q, x in enumerate(toks))
            lines.append(f"Dialogue: 0,{ass_time(a0)},{ass_time(a1)},Cap,,0,0,0,,{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def parse_facecam(spec: str, W: int, H: int):
    return layouts.parse_box(spec, W, H)


ASSETS = Path(__file__).resolve().parent / "assets"
FONTS = Path(__file__).resolve().parent / "fonts"


def _lum(arr) -> float:
    return float((0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]).mean() / 255.0)


def themed_spec(base: dict, imgs: list, n: int) -> dict:
    """Tweet layout on a picture background: picks the picture for clip n (they take turns) and chooses dark or
    light text by measuring the picture where the tweet text and the captions sit."""
    if not imgs:
        return base
    import copy
    path = imgs[(n - 1) % len(imgs)]
    spec = copy.deepcopy(base)
    spec["bg"] = dict(type="image", path=str(path))
    try:
        arr = layouts.image_array(path)
    except Exception as ex:  # noqa: BLE001
        log(f"  background image not usable ({str(ex)[:100]}), using the plain layout")
        return base
    th = (spec.get("themes") or {}).get(Path(str(path)).stem)
    if th:
        spec["title"].update(color=th["title"])
        spec["caption"].update(color=th["cap"], highlight=th["hi"])
        for L in spec.get("layers", []):
            L["shadow"] = dict(px=30, alpha=.22)
        return spec
    top_dark = _lum(arr[230:600]) < 0.52
    cap_dark = _lum(arr[1100:1450]) < 0.52
    if spec.get("title"):
        spec["title"].update(color="#FFFFFF" if top_dark else "#14102B",
                             header_color="#E4DFFF" if top_dark else "#5A4A78", max_chars=30)
    if spec.get("caption"):
        spec["caption"].update(color="#FFFFFF" if cap_dark else "#14102B",
                               highlight="#FFE066" if cap_dark else "#6D28D9")
    for L in spec.get("layers", []):
        if L.get("border"):
            L["border"] = dict(px=4, color="#FFFFFF")
        L["shadow"] = dict(px=30, alpha=.28)
    return spec


def ensure_fonts(cwd: Path) -> bool:
    """Copy the bundled Inter files next to the clip so ffmpeg's subtitle renderer can find them."""
    if not FONTS.is_dir() or not any(FONTS.glob("*.[ot]tf")):
        return False
    dst = cwd / "fonts"
    dst.mkdir(exist_ok=True)
    for f in FONTS.glob("*.[ot]tf"):
        if not (dst / f.name).exists():
            shutil.copy2(f, dst / f.name)
    return True
MUSIC_FILE = ""  # set by the editor to force one track from assets/music
HYPE_CATS = {"funny", "fail", "clutch", "rage"}


def pick_asset(kind: str, name: str = "", cat: str = ""):
    """assets/sfx/<name>.* or a music bed matching the mood (hype_*, tense_*); any file you drop in works."""
    d = ASSETS / kind
    if kind == "music" and MUSIC_FILE and (d / MUSIC_FILE).is_file():
        return d / MUSIC_FILE
    if not d.is_dir():
        return None
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".ogg", ".flac"))
    if kind == "sfx":
        files = [p for p in files if p.stem.lower().startswith(name)]
    else:
        mood = "hype" if cat in HYPE_CATS else "tense"
        files = [p for p in files if p.stem.lower().startswith(mood)] or files
        if files:
            files = [files[hash(cat) % len(files)]]
    return files[0] if files else None


def polish_graph(dur: float, peak_rel, n_in: int, cat: str, a):
    """Returns (zoom filter or '', audio filtergraph or '', extra ffmpeg input args). Zoom: a short punch-in on the
    peak. Audio: whoosh at the start, impact on the peak, quiet music that ducks under the voice, then loudnorm."""
    zoom = ""
    if not (not a.zoom) and peak_rel is not None and 0.8 < peak_rel < dur - 0.4:
        zoom = (f"scale=w='2*trunc(1080*(1+{a.zoom_amount}*max(0\\,1-abs(t-{peak_rel:.2f})/0.32))/2)':"
                f"h='2*trunc(1920*(1+{a.zoom_amount}*max(0\\,1-abs(t-{peak_rel:.2f})/0.32))/2)':eval=frame,crop=1080:1920")
    ex, chain, mix, k = [], [], ["[vo]"], n_in
    whoosh, impact = pick_asset("sfx", "whoosh"), pick_asset("sfx", "impact")
    music = None if a.no_music else pick_asset("music", cat=cat)
    if a.no_sfx:
        whoosh = impact = None
    if whoosh:
        ex += ["-i", str(whoosh)]; chain.append(f"[{k}:a]volume=0.55[sw]"); mix.append("[sw]"); k += 1
    if impact and peak_rel is not None and 0.5 < peak_rel < dur - 0.3:
        ms = int(max(0, peak_rel - 0.12) * 1000)
        ex += ["-i", str(impact)]; chain.append(f"[{k}:a]adelay={ms}|{ms},volume=0.6[si]"); mix.append("[si]"); k += 1
    duck = ""
    if music:
        ex += ["-stream_loop", "-1", "-i", str(music)]
        chain.append(f"[{k}:a]atrim=0:{dur:.2f},volume={a.music_vol},afade=t=in:d=0.4[mu]")
        duck = "[vo]asplit[vo][vs];[mu][vs]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[md];"
        mix.append("[md]"); k += 1
    if len(mix) == 1:
        return zoom, "", []
    base = f"[0:a]afade=t=in:d=0.06[vo]"
    graph = (base + ";" + ";".join(chain) + ";" if chain else base + ";")
    graph = graph.replace(f"{base};", f"{base};{duck}", 1) if duck else graph
    graph += "".join(mix) + f"amix=inputs={len(mix)}:duration=first:dropout_transition=0,"
    graph += f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={max(0.0, dur - 0.6):.2f}:d=0.6[a]"
    return zoom, graph, ex


def render(src: Path, info: dict, s: float, e: float, ass_name, out_name: str, a, cwd: Path,
           peak_rel=None, cat: str = "") -> None:
    g = layouts.build(a._spec, info["w"], info["h"], a._facecam_box, a.fps, e - s, a._cache,
                      base_dir=a.layouts_dir, look=a.look, frame=a.frame)
    n_in = 1 + sum(1 for extra in g["inputs"] for x in extra if x == "-i")
    zoom, agraph, aex = polish_graph(e - s, peak_rel, n_in, cat, a) if info["audio"] else ("", "", [])
    _sh = a._spec.get("sharpen")
    _pre = ""
    if _sh and info["audio"] is not None:
        _pre = f"[v0]split[sa][sb];[sa]crop=1080:{int(_sh['h'])}:0:{int(_sh['y'])},cas={_sh.get('cas', .7)},unsharp=5:5:{_sh.get('un', .5)}:5:5:0.0[ss];[sb][ss]overlay=0:{int(_sh['y'])}[v0s];"
    last = _pre + (f"[v0{'s' if _pre else ''}]ass='{str(ass_name).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" + (":fontsdir=fonts" if ensure_fonts(cwd) else "")) if ass_name else "[v0]null"
    emo = TITLE_EMO if (ass_name and TITLE_EMO and Path(TITLE_EMO["path"]).exists()) else None
    if emo:
        ei = n_in + sum(1 for x in aex if x == "-i")
        last += f"[vt];[{ei}:v]scale={emo['size']}:{emo['size']}:flags=lanczos,format=rgba[em];[vt][em]overlay={emo['x']}:{emo['y']}"
    fc = "[0:v]setpts=PTS-STARTPTS[vsrc];" + g["graph"].replace("[0:v]", "[vsrc]", 1) + ";" + last + (f",{zoom}[v]" if zoom else "[v]")
    if agraph:
        fc += ";" + agraph
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(src.resolve())]
    for extra in g["inputs"]:
        cmd += extra
    cmd += aex
    if emo:
        cmd += ["-i", emo["path"]]
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    if agraph:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "256k"]
    elif info["audio"]:
        cmd += ["-map", "0:a:0", "-af", f"afade=t=in:d=0.06,loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={max(0.0, e - s - 0.6):.2f}:d=0.6", "-c:a", "aac", "-b:a", "256k"]
    else:
        cmd += ["-an"]
    if a.encoder == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high"]
    else:
        cmd += ["-c:v", a.encoder, "-b:v", "8M"]
    for _k, _v in (getattr(a, "_meta", None) or {}).items():
        cmd += ["-metadata", f"{_k}={_v}"]
    cmd += ["-r", str(a.fps), "-t", f"{e - s:.3f}", "-shortest", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_name]
    sh(cmd, cwd=str(cwd))


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------
def auto_facecam(src, info, out, spec) -> str:
    """Find the streamer's face and return a crop box 'x:y:w:h' shaped like the layout's face slot ('' if none)."""
    f = out / "facecam_box.txt"
    if f.exists():
        return f.read_text().strip()
    box = ""
    try:
        face = av_signals.find_face(src, info["dur"], info["w"], info["h"], log)
        if face:
            W, H = info["w"], info["h"]
            x, y, w, h = face
            L = next((l for l in spec.get("layers", []) if l.get("src") == "face"), {})
            ar = (float(L.get("w", 1)) * 1080) / (float(L.get("h", .5)) * 1920)
            bh = min(H, h * 3.4)
            bw = bh * ar
            if bw > W:
                bw, bh = W, W / ar
            cx, cy = x + w / 2, y + h / 2 - h * 0.15
            if 0.3 * W < x + w / 2 < 0.7 * W and 0.25 * H < y + h / 2 < 0.75 * H:
                # webcams sit near the edges; a "face" dead centre is usually an avatar or a game character
                log("  face found mid-screen, probably not the webcam: using the layout without facecam")
                f.write_text("")
                return ""
            bx, by = max(0, min(W - bw, cx - bw / 2)), max(0, min(H - bh, cy - bh / 2))
            box = ":".join(str(int(v) // 2 * 2) for v in (bx, by, bw, bh))
            log(f"Facecam box for the layout: {box}")
    except Exception as ex:  # noqa: BLE001
        log(f"  could not find the facecam ({str(ex)[:100]}), using the layout without it")
    f.write_text(box)
    return box


def full_transcript(a, src, info, out):
    """Whisper over the whole video once (cached). Returns segments with word timings."""
    st = src.stat()
    cache = out / f"transcript_{st.st_size}_{int(st.st_mtime)}_v3{'_' + os.environ['CLIPPER_LANG'] if os.environ.get('CLIPPER_LANG') else ''}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)
    wav = tmp / "whole.wav"
    t0 = time.time()
    log("Transcribing the whole video with Whisper (local, free)...")
    if not extract_wav(src, 0.0, info["dur"], wav):
        return []
    PROG["file"], PROG["t0"] = out / "transcribe_progress.json", 0.0
    segs = Whisper(a.whisper_model, a.device, a.vocab).run(wav, 0.0)
    tprogress(info["dur"], info["dur"])
    PROG["file"] = None
    try:
        wav.unlink()
    except OSError:
        pass
    _t = Path(str(cache) + ".tmp")
    _t.write_text(json.dumps(segs), encoding="utf-8")
    os.replace(_t, cache)
    log(f"  {len(segs)} lines in {time.time() - t0:.0f}s")
    return segs


def scout_pass(a, model, src, info, out, comb, picks, strong):
    """Claude reads the full transcript and names moments; merged with the sound/chat/face picks."""
    segs = full_transcript(a, src, info, out)
    if not segs:
        return None, picks, strong
    tags = lambda s0, e0: (a._av_tags(s0, e0) if a._av_tags else [])  # noqa: E731
    log(f"Claude is reading the whole transcript ({int(info['dur'] // 60)} min) to find moments...")
    found = brain.scout(model, segs, info["dur"], a.video_title or src.stem, a.channel, a.min_len, a.max_len,
                        (a.brain_topic or (GTA_TOPIC if a.gta else "")) + getattr(a, "_trend_topic", ""), tags, log=log)
    found = sorted((m for m in found if m["strength"] >= 40), key=lambda m: -m["strength"])
    log(f"  Claude named {len(found)} moment(s) worth a closer look"
        + (": " + ", ".join(f"{hms(m['t'])} ({m['strength']})" for m in found[:8]) if found else ""))
    merged = []
    sig = sorted(picks, key=lambda p: -p["score"])
    cand = [dict(t=float(m["t"]), score=float(comb[min(len(comb) - 1, int(m["t"]))]) if len(comb) else 1.0,
                 groups={"claude": m["strength"] / 10}, active=["claude: " + m["why"]]) for m in found]
    # alternate: best Claude pick, best signal pick, ...
    order = [x for pair in zip(cand, sig) for x in pair] + cand[len(sig):] + sig[len(cand):]
    for c in order:
        if any(abs(c["t"] - p["t"]) < a.min_gap for p in merged):
            continue
        merged.append(c)
        if len(merged) >= a.judge_max:
            break
    return segs, merged, len(merged)



def ai_pass(rows, a, model, src, info, whole, kw_segs, whisper, sigs, tmp, want):
    """Let the local AI read the words around each shortlisted moment. It keeps the clips worth watching, sets
    clean start and end points, and writes the hook and tweet text. Moments it rejects are only used to fill up
    to the requested count, and are marked as weaker."""
    aud = sigs.get("audio")
    loud_at = (lambda s0, e0: bool(aud is not None and len(aud) and aud[int(s0):int(e0) + 1].size
                                   and aud[int(s0):int(e0) + 1].max() > 1.5))
    loud_bool = loud_at
    loud_at = lambda s0, e0: ((["LOUD"] if loud_bool(s0, e0) else [])  # noqa: E731
                              + (a._av_tags(s0, e0) if a._av_tags else []))
    topic = (a.brain_topic or (GTA_TOPIC if a.gta else "")) + getattr(a, "_trend_topic", "")
    title = a.video_title or Path(str(src)).stem
    log(f"\nAI judge reading {len(rows)} moments...")
    for r in rows:
        s, e, peak = r["_s"], r["_e"], r["_c"]["t"]
        lo, hi = max(0.0, s - 35), min(info["dur"], e + 35)
        ctx = whole or ([] if whisper else kw_segs) or []
        if not ctx and whisper:
            wav = tmp / f"ctx{r['clip']}.wav"
            if extract_wav(src, lo, hi, wav):
                ctx = whisper.run(wav, lo)
                r["_segs"] = ctx
        j = brain.judge(model, ctx, lo, hi, peak, title, a.channel, a.min_len, a.max_len, topic, loud_at, video=src) if ctx else None
        r["_j"] = j
        if j is None:
            log(f"  {r['peak']}: the AI could not judge this one, keeping the sound score")
            continue
        words = [w for sg in ctx for w in sg.get("words", [])]
        if j["keep"]:
            cp = brain.cut_points(j, words, ctx, lo, hi, peak, a.min_len, a.max_len, info["dur"])
            if cp:
                r["_s"], r["_e"], r["_ai_cut"] = cp[0], cp[1], True
        log(f"  {r['peak']}: {'KEEP' if j['keep'] else 'skip'}  {j['score']}/100  {j['category']}  {j['reason'][:70]}")
    # rank: AI-kept first by AI score, then the rest (so the requested count is still reached)
    def key(r):
        j = r.get("_j")
        if j and j["keep"]:
            return (0, -j["score"])
        return (1, -(j["score"] if j else int(r["score"])))
    rows.sort(key=key)
    kept_rows = [r for r in rows if r.get("_j") and r["_j"]["keep"]]
    if getattr(a, "all_keepers", False) and len(kept_rows) > want:
        log(f"Rendering all {len(kept_rows)} keepers (--all-keepers).")
        want = len(kept_rows)
    _cap = int(os.environ.get("CLIPPER_MAX_RENDER", "0") or 0)  # one-off limit, set by run_streamer from max_clips_once.txt
    if _cap and want > _cap:
        want = _cap
        log(f"Rendering only the top {_cap} (one-off limit).")
    if len(kept_rows) > want and model.lower().startswith("claude:") and not os.environ.get("CLIPPER_JUDGE_MODEL"):
        ids = brain.rank(model, [dict(id=i, category=r["_j"]["category"], score=r["_j"]["score"],
                                      reason=r["_j"]["reason"], tweet=r["_j"].get("tweet", ""))
                                 for i, r in enumerate(kept_rows)])
        if ids:
            pos = {i: k for k, i in enumerate(ids)}
            kept_rows = [r for _, r in sorted(enumerate(kept_rows), key=lambda ir: (pos.get(ir[0], 99), ir[0]))]
            log("Claude compared the keepers side by side: " + ", ".join(r["peak"] for r in kept_rows[:want])
                + " come out on top.")
            rows = kept_rows + [r for r in rows if r not in kept_rows]
    chosen = rows[:want]
    if not a.fill:
        chosen = [r for r in chosen if r.get("_j") and r["_j"]["keep"]]
    kept = sum(1 for r in chosen if r.get("_j") and r["_j"]["keep"])
    if kept < len(chosen):
        log(f"The AI is convinced by {kept} of {len(chosen)}; the rest are added only to reach {want} (marked 'AI unsure').")
    if not chosen:
        log('No moment passed the AI judge. Nothing rendered (move to the next streamer).')
    chosen.sort(key=lambda r: r["_c"]["t"])
    for n_, r in enumerate(chosen, 1):
        j = r.get("_j")
        r["clip"] = n_
        if j:
            r["score"] = str(j["score"])
            r["category"], r["hook"], r["tweet"] = j["category"], j["hook"], j["tweet"]
            r["why"] = ("" if j["keep"] else "AI unsure, ") + f"AI: {j['reason']} | " + r["why"]
        r["start"], r["end"] = hms(r["_s"]), hms(r["_e"])
    return chosen


def write_report(out: Path, rows: list) -> None:
    cols = ["clip", "peak", "start", "end", "score", "chat", "audio", "text", "category", "hook", "tweet", "why", "file"]
    with open(out / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
    cards = []
    for r in rows:
        vid = (f'<video controls preload="metadata" src="{html.escape(r["file"])}"></video>'
               if r.get("file") else '<div class="nov">not rendered</div>')
        cards.append(
            f'<div class="card">{vid}<div class="meta"><b>#{html.escape(str(r["clip"]))}</b> '
            f'score {r["score"]}/100<br>source {r["start"]} to {r["end"]} (peak {r["peak"]})<br>'
            f'<span>{html.escape(r["why"])}</span></div></div>')
    page = ("<!doctype html><meta charset='utf-8'><title>Clips</title><style>"
            "body{font:14px system-ui;background:#111;color:#eee;margin:20px}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}"
            ".card{background:#1b1b1b;border-radius:10px;padding:10px}video{width:100%;border-radius:8px;"
            "background:#000;aspect-ratio:9/16}.meta{margin-top:8px;line-height:1.5}.meta span{color:#9aa}"
            ".nov{aspect-ratio:9/16;display:grid;place-items:center;color:#777}</style>"
            "<h2>Clips to review</h2><p>Move the good ones into a folder called approved, then upload.</p>"
            f"<div class='grid'>{''.join(cards)}</div>")
    (out / "index.html").write_text(page, encoding="utf-8")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Find viral moments and cut vertical captioned clips (local).")
    p.add_argument("input", help="video file path or URL")
    p.add_argument("--chat", help="chat file: TwitchDownloader .json, yt-dlp live_chat .json, or .csv (time,user,message)")
    p.add_argument("--transcript", help="optional .srt/.vtt/.json for the whole video (skips Whisper)")
    p.add_argument("--signal-transcript", help="whole-video .vtt/.srt used ONLY to find keywords (captions still use Whisper)")
    p.add_argument("--keywords", default="", help="extra spoken words that mark a moment, regex style: vice city|lucia|graphics")
    p.add_argument("--out", default="out", help="output folder (default: out)")
    p.add_argument("--at", default="", help="force the peak time(s), HH:MM:SS, comma separated (skips auto detection)")
    p.add_argument("--top", type=int, default=10, help="max clips to make")
    p.add_argument("--all-keepers", action="store_true",
                   help="render every moment the AI judge keeps, not just the best --top")
    p.add_argument("--pre", type=float, default=30, help="seconds before the peak to include")
    p.add_argument("--post", type=float, default=12, help="seconds after the peak to include")
    p.add_argument("--min-len", type=float, default=15)
    p.add_argument("--max-len", type=float, default=75)
    p.add_argument("--min-gap", type=float, default=60, help="minimum seconds between clip peaks")
    p.add_argument("--skip-start", type=float, default=0, help="ignore the first N seconds (intros)")
    p.add_argument("--min-score", type=float, default=1.0, help="raise to keep only stronger moments")
    p.add_argument("--agree", type=int, default=2, help="how many signal groups must fire together")
    p.add_argument("--agree-z", type=float, default=1.5, help="how strong a group must be to count as fired")
    p.add_argument("--w-chat", type=float, default=0.4)
    p.add_argument("--w-audio", type=float, default=0.3)
    p.add_argument("--w-text", type=float, default=0.3)
    p.add_argument("--w-vocal", type=float, default=1.0, help="weight of laughing / screaming detection")
    p.add_argument("--w-face", type=float, default=0.9, help="weight of facecam reactions")
    p.add_argument("--language", default="", help="force the speech language, e.g. hi for Hindi (default: auto)")
    p.add_argument("--no-av", action="store_true", help="skip the laugh/scream and facecam detectors")
    p.add_argument("--no-face", action="store_true", help="skip only the facecam detector")
    p.add_argument("--no-scout", action="store_true", help="do not let Claude read the whole transcript")
    p.add_argument("--judge-max", type=int, default=12, help="most moments the AI judges per video")
    p.add_argument("--hype-words", default="", help="extra chat words, regex style: word1|word2")
    p.add_argument("--layout", default="blur", help="layout name (see layouts.py or your layouts folder)")
    p.add_argument("--facecam", help="facecam box x:y:w:h in pixels or 0-1 fractions (layouts that show the face)")
    p.add_argument("--look", help="colour grade: none, punchy, cinematic, warm, cool, neon, vice, crisp")
    p.add_argument("--frame", help="frame overlay: none, neon, cyan, white, gold, soft, or a PNG in layouts/frames")
    p.add_argument("--layouts-dir", default=str(Path(__file__).resolve().parent / "layouts"),
                   help="folder with your own layout .json files and frames/")
    p.add_argument("--list-layouts", action="store_true", help="print the available layouts and exit")
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--caption-style", choices=["karaoke", "plain"], default="karaoke")
    p.add_argument("--caption-color", default="#FFE500", help="karaoke highlight colour")
    p.add_argument("--no-extend", action="store_true", help="fixed clip length, do not extend while hype lasts")
    p.add_argument("--exact", action="store_true", help="do not relax the bar to reach --top clips")
    p.add_argument("--whisper-model", default="auto", help="auto (large-v3-turbo on a GPU, small on CPU), tiny, base, small, medium, large-v3")
    p.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--vocab", default="", help="words Whisper should know, e.g. 'GTA VI, Vice City'")
    p.add_argument("--font", default="Inter", help="caption and title font (Inter files are bundled in fonts/)")
    p.add_argument("--bg-image", nargs="+", default=[], help="picture(s) used as the tweet layout background, one per clip in turn")
    p.add_argument("--title", default="", help="tweet style headline shown on the clip (layouts with a title block, e.g. tweet)")
    p.add_argument("--title-by", default="", help="small header above the title, e.g. 'IShowSpeed @ishowspeed'")
    p.add_argument("--brain", choices=["auto", "ollama", "off"], default="auto",
                   help="local AI judge (Ollama) that reads the words around each moment. auto = use it when it is running")
    p.add_argument("--brain-model", default="qwen2.5:14b", help="Ollama model name")
    p.add_argument("--hook", action="store_true", help="pop-in hook text at the start (off by default)")
    p.add_argument("--zoom", action="store_true", help="punch-in zoom on the peak (off by default)")
    p.add_argument("--zoom-amount", type=float, default=0.07, help="zoom size at the peak (0.07 = 7 percent)")
    p.add_argument("--no-sfx", action="store_true", help="no whoosh or impact sound effects")
    p.add_argument("--no-music", action="store_true", help="no background music")
    p.add_argument("--music-vol", type=float, default=0.14, help="music level before ducking (0.14 is quiet)")
    p.add_argument("--fill", action="store_true", help="pad with AI-unsure clips to reach --top")
    p.add_argument("--strict", action="store_true", help="with --at: render only moments the AI keeps (live mode)")
    p.add_argument("--skip-top", type=int, default=0, help="drop the best N clips after ranking (to get the next best)")
    p.add_argument("--gta", action="store_true", help="GTA VI layer: spicy/romance, cameo and hot-take keywords plus a matching AI brief")
    p.add_argument("--brain-topic", default="", help="what you care about, e.g. 'GTA 6 reactions' (nudges the AI)")
    p.add_argument("--video-title", default="", help="title of the source video (context for the AI)")
    p.add_argument("--channel", default="", help="channel name (context for the AI)")
    p.add_argument("--hinglish", action="store_true", help="Hindi speech, captions in Hinglish (Hindi in Roman letters)")
    p.add_argument("--resub", default="", help="redo the captions of one clip with the current subtitle logic, then re-render it")
    p.add_argument("--restyle", action="store_true", help="with --resub: switch the clip to the split layout (N3ON look)")
    p.add_argument("--redo", default="", help="re-render one clip from its .edit.json (caption editor)")
    p.add_argument("--no-fix-captions", action="store_true", help="do not let the AI tidy caption spelling")
    p.add_argument("--no-upper", action="store_true", help="do not uppercase captions")
    p.add_argument("--encoder", default="libx264", help="e.g. h264_nvenc, h264_videotoolbox")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--dry-run", action="store_true", help="only list the picked moments, no rendering")
    return p


THEMES_PY = {"Peach": dict(base="bg_1.png", alpha=.28, title="#2B1636", cap="#2B1636", hi="#7A2BD9"),
             "Dusk": dict(base="bg_2.png", alpha=.18, title="#FFFFFF", cap="#FFFFFF", hi="#FFE066"),
             "Black": dict(base="base_black.png", alpha=.08, title="#F4F4F5", cap="#F4F4F5", hi="#FFE066"),
             "Grey": dict(base="base_grey.png", alpha=.30, title="#151517", cap="#151517", hi="#6D28D9")}


def apply_params(a, side: dict, out: Path) -> dict:
    """Editor settings (video box, title, captions, theme) -> a layout spec for the manrope_card layout."""
    import copy
    import hashlib
    from PIL import Image, ImageDraw
    p = side["p"]
    th = THEMES_PY.get(p.get("theme", "Peach"), THEMES_PY["Peach"])
    v, ti, cp = p["video"], p["title"], p["caption"]
    x, y, w, h = float(v["x"]), float(v["y"]), float(v["w"]), float(v["h"])
    pad, rd = int(p.get("pad", 18)), int(v.get("round", 30))
    rect = (round(x - pad), round(y - pad), round(x + w + pad), round(y + h + pad))
    alpha = float(p["alpha"]) if p.get("alpha") not in (None, "") else th["alpha"]
    ccol = str(p.get("card_color") or "#FFFFFF").lstrip("#")
    ccol = tuple(int(ccol[i:i + 2], 16) for i in (0, 2, 4)) if len(ccol) == 6 else (255, 255, 255)
    key = hashlib.md5(json.dumps([p.get("theme"), rect, rd, alpha, ccol], sort_keys=True).encode()).hexdigest()[:12]
    path = out / "_layout_cache" / f"card_{key}.png"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        base = Image.open(ASSETS / th["base"]).convert("RGBA").resize((1080, 1920))
        ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).rounded_rectangle(rect, radius=rd + pad, fill=ccol + (round(255 * alpha),))
        Image.alpha_composite(base, ov).convert("RGB").save(path)
    spec = copy.deepcopy(a._spec0)
    spec["bg"] = dict(type="image", path=str(path))
    L = spec["layers"][0]
    L.update(cx=(x + w / 2) / 1080, cy=(y + h / 2) / 1920, w=w / 1080, round=rd, fit="contain", shadow=dict(px=int(v.get("shadow", 30)), alpha=.22 if int(v.get("shadow", 30)) else 0))
    L.pop("h", None)
    spec["title"].update(x=int(ti["x"]), y=int(ti["y"]), size=int(ti["size"]), color=ti.get("color") or th["title"],
                         max_chars=int(ti.get("max_chars", 18)))
    if ti.get("font"):
        spec["title"].update(font=ti["font"], bold=int(ti.get("bold", 0)))
    spec["caption"].update(size=int(cp["size"]), color=cp.get("color") or th["cap"], highlight=cp.get("hi") or th["hi"],
                           outline=int(cp.get("outline", 0)), shadow=int(cp.get("shadow", 0)), upper=bool(cp.get("upper", False)))
    if cp.get("font"):
        spec["caption"].update(font=cp["font"], bold=int(cp.get("bold", -1)))
    spec["caption_y"] = max(0, 1920 - int(cp["y"]))
    return spec


def redo(side_path: str) -> None:
    """Re-render one clip with edited caption words (written by the Clipdesk caption editor)."""
    side = json.loads(Path(side_path).read_text(encoding="utf-8"))
    a = build_parser().parse_args(side["argv"])
    if a.hinglish and not a.language:
        a.language = "hi"
    need("ffmpeg")
    need("ffprobe")
    all_layouts, _ = layouts.load_layouts(a.layouts_dir)
    out = Path(side["out"])
    src = Path(side["input"])
    info = probe(src)
    a._facecam_box = layouts.parse_box(a.facecam, info["w"], info["h"]) if a.facecam else None
    if side.get("p"):
        a.layout = "manrope_card"
    a._spec0, _ = layouts.resolve(a.layout, all_layouts, a._facecam_box is not None)
    a._cache = out / "_layout_cache"
    a._av_tags = None
    if side.get("p"):
        a._spec = apply_params(a, side, out)
        global MUSIC_FILE
        pp = side["p"]
        a.no_music = not pp.get("music")
        a.no_sfx = not (pp.get("sfx") if pp.get("sfx") is not None else pp.get("music"))
        MUSIC_FILE = Path(str(pp.get("music_track") or "")).name
        if pp.get("music_vol") is not None:
            a.music_vol = float(pp["music_vol"])
    else:
        if side.get("bg"):
            a.bg_image = [side["bg"]]
        a._spec = themed_spec(a._spec0, a.bg_image, side["i"] if not side.get("bg") else 1)
    s, e, base = side["s"], side["e"], side["base"]
    peak = side.get("peak", s + (side.get("peak_rel") or 0))
    words = [dict(start=w["start"], end=w["end"], word=w["word"]) for w in side["words"]
             if str(w["word"]).strip() and w["end"] > s and w["start"] < e]
    tweet = side.get("tweet") or ""
    if side.get("info"):
        a._meta = {"title": tweet, "comment": side["info"]}
    ass_name = None
    if build_ass(words if not a.no_captions else [], s, out / f"{base}.ass", a.font, not a.no_upper,
                 a._spec.get("caption_y", 520), a.caption_style, a.caption_color, a._spec.get("caption"),
                 tweet if a._spec.get("title") else "", a.title_by, a._spec.get("title"), e - s,
                 "" if (not a.hook) else (side.get("hook") or ""), a._spec.get("hook")):
        ass_name = f"{base}.ass"
    tmp_name = f"{base}.redo.mp4"
    render(src, info, s, e, ass_name, tmp_name, a, out, peak - s, side.get("cat", ""))
    for _try in range(30):  # the dashboard may be reading the old file for a moment
        try:
            os.replace(out / tmp_name, out / f"{base}.mp4")
            break
        except PermissionError:
            time.sleep(2)
    else:
        raise PermissionError(f"{base}.mp4 is locked")
    log(f"redone {base}.mp4 ({len(words)} caption words)")


def restyle_argv(side: dict, layout: str = "split") -> list:
    """The N3ON look for every clip: half facecam / half gameplay, no gradient backgrounds."""
    argv, out, skip = list(side["argv"]), [], 0
    i = 0
    while i < len(argv):
        x = argv[i]
        if x in ("--layout", "--title-by"):
            i += 2
            continue
        if x == "--bg-image":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                i += 1
            continue
        out.append(x)
        i += 1
    ch = out[out.index("--channel") + 1] if "--channel" in out else ""
    if not ch:  # runs/live/<Name>_<date>_<time>/...
        for par in Path(side["input"]).parents:
            m_ = re.match(r"(.+)_\d{8}_\d{6}$", par.name)
            if m_:
                ch = m_.group(1).replace("_", " ")
                break
    cre = {}
    try:
        cr = json.loads((Path(__file__).resolve().parent / "creators.json").read_text(encoding="utf-8"))
        cre = next((c for c in cr.get("creators", []) if ch and c.get("name", "").lower() == ch.lower()), {})
    except Exception:  # noqa: BLE001
        pass
    if layout != "title_card_game":  # gameplay-only runs keep their layout; no per-streamer override
        layout = cre.get("layout") or layout
    out += ["--layout", layout]
    if cre.get("language") == "hi" and "--hinglish" not in out:
        out.append("--hinglish")  # Hindi streamer: Hinglish captions even if the clip was first made as English
    if "--facecam" not in out and layouts.load_layouts(str(Path(__file__).resolve().parent / "layouts"))[0][layout].get("needs_face"):
        box = cre.get("facecam", "")
        if not box:
            src, o = Path(side["input"]), Path(side["out"])
            all_layouts, _ = layouts.load_layouts(str(Path(__file__).resolve().parent / "layouts"))
            box = auto_facecam(src, probe(src), o, all_layouts[layout])
        if box:
            out += ["--facecam", box]
    side.pop("p", None)
    side.pop("bg", None)
    return out


def resub(side_path: str, restyle: bool = False) -> None:
    """Redo one clip's subtitles from its own audio with the current logic (Parakeet for English,
    short-window Whisper + on-screen names for Hindi), save them in the sidecar, then re-render."""
    sp = Path(side_path)
    side = json.loads(sp.read_text(encoding="utf-8"))
    if restyle:
        side["argv"] = restyle_argv(side)
    a = build_parser().parse_args(side["argv"])
    if a.hinglish and not a.language:
        a.language = "hi"
    if a.language:
        os.environ["CLIPPER_LANG"] = a.language
    need("ffmpeg")
    need("ffprobe")
    src, s, e = Path(side["input"]), side["s"], side["e"]
    out = Path(side["out"])
    tmp = out / "_tmp_resub"
    tmp.mkdir(exist_ok=True)
    try:
        w = Whisper(a.whisper_model, a.device, a.vocab)
        w.src = src
        wav = tmp / "r.wav"
        if not extract_wav(src, s, e, wav):
            log("  no audio, skipped")
            return
        segs = w.run(wav, s)
        words = [x for sg in segs for x in sg.get("words", []) if x["end"] > s and x["start"] < e]
        if a.language == "hi" and words:
            words = romanize_words(words)
            names = ocr_names(src, s, e - s)
            if names:
                log("  on-screen names: " + ", ".join(names))
                words = snap_names(words, names)
            if a.hinglish and a.brain != "off":
                try:
                    ok_, bm = brain.available(a.brain_model)
                    if ok_:
                        words = brain.hinglish_captions(bm, words, (a.video_title or src.stem)
                                                        + (" | names on screen: " + ", ".join(names) if names else ""))
                        words = romanize_words(words)
                        if names:
                            words = snap_names(words, names)
                except Exception as ex:  # noqa: BLE001
                    log(f"  hinglish pass skipped ({str(ex)[:80]})")
            words = tidy_words(words)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not words:
        log("  no words found, kept the old captions")
        return
    side["words"] = [dict(start=x["start"], end=x["end"], word=x["word"]) for x in words]
    sp.write_text(json.dumps(side, ensure_ascii=False), encoding="utf-8")
    log("  " + " ".join(x["word"] for x in words))
    redo(side_path)


def main() -> None:
    T0 = time.time()
    if "--resub" in sys.argv:
        i_ = sys.argv.index("--resub")
        return resub(sys.argv[i_ + 1], "--restyle" in sys.argv)
    if "--redo" in sys.argv:
        i_ = sys.argv.index("--redo")
        return redo(sys.argv[i_ + 1])
    a = build_parser().parse_args()
    if a.hinglish and not a.language:
        a.language = "hi"
    if a.language:
        os.environ["CLIPPER_LANG"] = a.language
        if a.language == "hi":
            os.environ.setdefault("CLIPPER_PROMPT", "Yaar ye kya hai bhai, are bhai dekho, chalo GTA gameplay shuru karte hain, arre wah.")
    need("ffmpeg")
    need("ffprobe")
    all_layouts, warns = layouts.load_layouts(a.layouts_dir)
    if a.list_layouts:
        for d in layouts.describe(all_layouts):
            print(f"{d['name']:14s} {'(needs facecam) ' if d['needs_face'] else ''}{d['desc']}")
        print("looks :", ", ".join(layouts.LOOKS))
        print("frames:", ", ".join(layouts.list_frames(a.layouts_dir)))
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    chat_path = Path(a.chat) if a.chat else None
    if re.match(r"^https?://", a.input):
        src, found = download(a.input, out / "download")
        chat_path = chat_path or found
    else:
        src = Path(a.input)
        if not src.exists():
            sys.exit(f"File not found: {src}")
    info = probe(src)
    for w_ in warns:
        log(w_)
    if not a.facecam and all_layouts.get(a.layout, {}).get("needs_face"):
        a.facecam = auto_facecam(src, info, out, all_layouts[a.layout])
        if a.facecam:
            sys.argv += ["--facecam", a.facecam]
    try:
        a._facecam_box = layouts.parse_box(a.facecam, info["w"], info["h"]) if a.facecam else None
        a._spec, note = layouts.resolve(a.layout, all_layouts, a._facecam_box is not None)
    except layouts.LayoutError as ex:
        log(str(ex))
        sys.exit(str(ex))
    a._spec0 = a._spec
    a._cache = out / "_layout_cache"
    if note:
        log(note)
    log(f"Layout: {a._spec.get('name', a.layout)}" + (f", look: {a.look}" if a.look else "")
        + (f", frame: {a.frame}" if a.frame else ""))
    n = int(math.ceil(info["dur"]))
    log(f"Video: {info['w']}x{info['h']}, {hms(info['dur'])}, audio={'yes' if info['audio'] else 'no'}")

    hype_re = re.compile(CHAT_HYPE + (("|" + a.hype_words) if a.hype_words else ""), re.I)
    try:
        import trending
        trending.refresh(log)
        _tw, a._trend_topic = trending.regex(), trending.topic()
    except Exception:  # noqa: BLE001 - the saved or built in words still work
        _tw, a._trend_topic = "", ""
    text_re = re.compile(TEXT_HYPE + (("|" + GTA_WORDS) if a.gta else "") + (("|" + a.keywords) if a.keywords else "")
                         + (("|" + _tw) if _tw else ""), re.I)
    kw_segs = []
    sigs, weights = {}, {"chat": a.w_chat, "audio": a.w_audio, "text": a.w_text, "vocal": a.w_vocal, "face": a.w_face}
    a._av_tags = None

    if chat_path:
        msgs = load_chat(chat_path)
        log(f"Chat: {len(msgs)} messages")
        if msgs:
            sigs["chat"] = chat_signal(msgs, n, hype_re)
        else:
            log("  chat file had no readable messages, ignoring it")
    else:
        log("No chat file given: using audio only (add --chat for better picks).")

    if info["audio"]:
        st = src.stat()
        cache = out / f"audio_db_{st.st_size}_{int(st.st_mtime)}.npy"
        db = audio_db_per_second(src, info["dur"], cache)
        loud = box(db, 2)
        loud[db < -55] = loud.min()  # digital silence is never a moment
        prev = np.concatenate([np.full(8, db[0]), box(db, 7)[:-8]]) if len(db) > 8 else db
        rise = np.clip(db - prev, 0, None)  # quiet, then a sudden shout
        rise[db < -55] = 0
        sigs["audio"] = np.maximum(rolling_z(loud, win=300), 0.8 * rolling_z(rise, win=300))

    if not a.no_av and not a.dry_run:
        ok_av, why_av = av_signals.available()
        if not ok_av:
            log(f"Laugh/scream and facecam detectors off: {why_av}")
        else:
            st = src.stat()
            key = f"{st.st_size}_{int(st.st_mtime)}"
            ve = fr = None
            try:
                if info["audio"]:
                    log("Listening for laughing, screaming and gunfire (local, free)...")
                    ve = av_signals.vocal_events(src, info["dur"], out / f"sounds_{key}.npz", log)
            except Exception as ex:  # noqa: BLE001
                log(f"  sound detector failed: {str(ex)[:160]}")
            try:
                if not a.no_face:
                    log("Watching the facecam for reactions (local, free)...")
                    fr = av_signals.face_reaction(src, info["dur"], info["w"], info["h"], out / f"face2_{key}.npz", log)
            except Exception as ex:  # noqa: BLE001
                log(f"  facecam detector failed: {str(ex)[:160]}")
            for k_, v_ in av_signals.signals(ve, fr, n, rolling_z).items():
                sigs[k_] = v_[:n] if len(v_) >= n else np.pad(v_, (0, n - len(v_)))
            a._av_tags = av_signals.tagger(ve, fr) if (ve is not None or fr is not None) else None

    whole = None
    if a.transcript:
        whole = load_transcript(Path(a.transcript))
        sigs["text"] = text_signal(whole, n, text_re)
        log(f"Transcript: {len(whole)} segments")

    elif a.signal_transcript and Path(a.signal_transcript).exists():
        kw_segs = load_signal_vtt(Path(a.signal_transcript))
        if kw_segs:
            sigs["text"] = text_signal(kw_segs, n, text_re)
            log(f"Keyword transcript: {len(kw_segs)} lines (used for the text signal only)")

    if not sigs:
        sys.exit("No usable signals: the video has no audio and no chat was given.")

    comb = combine(sigs, weights)
    if n / max(1.0, a.min_gap) < a.top * 1.5:  # short video: allow moments closer together
        a.min_gap = max(25.0, n / (a.top * 1.5))
    brain_model, brain_note = None, ""
    if a.brain != "off" and not a.dry_run:
        ok_, res_ = brain.available(a.brain_model)
        if ok_:
            brain_model = res_
        else:
            brain_note = res_
            if a.brain == "ollama":
                sys.exit(res_)
            log(f"AI judge off: {res_}")
    want = a.top
    if brain_model:
        a.top = max(a.top + 3, a.top * 2)  # shortlist more than we need, the AI keeps the best
        log(f"AI judge on: {brain_model} (shortlisting {a.top}, keeping the best {want})")
    picks = find_candidates(sigs, weights, a)
    strong = len(picks)
    # Not enough clear spikes? Lower the bar in steps until the requested number is reached, and say so.
    base_score, base_z = a.min_score, a.agree_z
    if not a.exact:
        for f in (0.7, 0.5, 0.35, 0.2, 0.1):
            if len(picks) >= a.top:
                break
            a.min_score, a.agree_z = base_score * f, base_z * f
            picks = find_candidates(sigs, weights, a, picks)
    a.min_score, a.agree_z = base_score, base_z
    a.top = want
    if a.at:
        picks = [dict(t=float(sum(float(x) * 60 ** i for i, x in enumerate(reversed(v.strip().split(":"))))),
                      score=9.0, groups={}, active=["manual"]) for v in a.at.split(",") if v.strip()]
        strong = len(picks)
        a.fill = not a.strict
    elif (brain_model and brain_model.lower().startswith("claude:") and not a.no_scout and info["audio"]
          and not a.transcript):
        whole, picks, strong = scout_pass(a, brain_model, src, info, out, comb, picks, strong)
    for k, pk in enumerate(picks):
        pk["weak"] = k >= strong
    if len(picks) > strong:
        log(f"{strong} moment(s) cleared the normal bar. Added {len(picks) - strong} weaker one(s) to reach "
            f"{len(picks)} of {a.top} asked (marked 'weaker'; use --exact to turn this off).")
    if not picks:
        sys.exit("No moments passed the filters. Try --agree 1, a lower --min-score or --agree-z, or add --chat.")
    picks.sort(key=lambda p: p["t"])
    log(f"\nPicked {len(picks)} moments:")
    for i, c in enumerate(picks, 1):
        g = " ".join(f"{k}={v:.1f}" for k, v in c["groups"].items())
        log(f"  {i:2d}. {hms(c['t'])}  score {score100(c['score'])}/100  [{g}]{'  weaker' if c.get('weak') else ''}")

    rows = []
    for i, c in enumerate(picks, 1):
        s, e = window(c["t"], a, info["dur"], comb, c["score"])
        said = []
        for sg in (kw_segs or whole or []):
            if s <= sg["start"] <= e:
                said += [m.group(0).lower() for m in text_re.finditer(sg.get("text", ""))]
        said = sorted(set(said), key=said.count, reverse=True)[:5]
        rows.append(dict(clip=i, peak=hms(c["t"]), start=hms(s), end=hms(e), score=str(score100(c["score"])),
                         chat=f"{c['groups'].get('chat', 0):.1f}", audio=f"{c['groups'].get('audio', 0):.1f}",
                         text=f"{c['groups'].get('text', 0):.1f}",
                         why=("weaker pick, " if c.get("weak") else "") + "fired: " + (", ".join(c["active"]) or "none")
                         + (("; said: " + ", ".join(said)) if said else ""), file="", _s=s, _e=e, _c=c))
    if a.dry_run:
        write_report(out, [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
        log(f"\nDry run done. See {out / 'report.csv'}")
        return

    whisper = None
    need_ctx = bool(brain_model) and not whole and not kw_segs
    if (not a.no_captions or need_ctx) and info["audio"]:  # captions always come from the clip's own audio
        whisper = Whisper(a.whisper_model, a.device, a.vocab)
    tmp = out / "_tmp"
    tmp.mkdir(exist_ok=True)
    if brain_model:
        rows = ai_pass(rows, a, brain_model, src, info, whole, kw_segs, whisper, sigs, tmp, want)
        if a.skip_top:
            rows = rows[a.skip_top:]

    failed = 0
    for r in rows:
        i, s, e = r["clip"], r["_s"], r["_e"]
        a._spec = themed_spec(a._spec0, a.bg_image, i)
        log(f"\nClip {i}/{len(rows)}: peak {r['peak']}")
        tc = time.time()
        segs = whole or []
        try:
            if whisper:
                have = r.get("_segs")
                if have and have[0].get("own") and not (r.get("_ai_cut") and (s < have[0]["start"] - 0.5 or e > have[-1]["end"] + 0.5)):
                    segs = have
                else:
                    ps, pe = max(0.0, s - 8), min(info["dur"], e + 8)
                    wav = tmp / f"w{i}.wav"
                    if extract_wav(src, ps, pe, wav):
                        segs = whisper.run(wav, ps)
                        for sg in segs:
                            sg["own"] = True
                        r["_segs"] = segs
            if segs and not r.get("_ai_cut"):
                s, e = snap(s, e, segs, a, info["dur"])
            words = [w for sg in segs for w in sg.get("words", []) if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
            from_pk = any(sg.get("eng") == "pk" for sg in segs)
            if from_pk and whisper:  # final pass on exactly the clip, the way the benchmark clip was made
                wav = tmp / f"x{i}.wav"
                if extract_wav(src, s, e, wav):
                    exact = whisper.run(wav, s)
                    ew = [w for sg in exact for w in sg.get("words", [])]
                    if ew:
                        words = ew
            if a.language == "hi" and words:
                words = romanize_words(words)
                names = ocr_names(src, s, e - s)
                if names:
                    log("  on-screen names: " + ", ".join(names))
                    words = snap_names(words, names)
                if brain_model and a.hinglish:
                    words = brain.hinglish_captions(brain_model, words, (a.video_title or src.stem)
                                                    + (" | names on screen: " + ", ".join(names) if names else ""))
                    words = romanize_words(words)
                    if names:
                        words = snap_names(words, names)
            elif brain_model and words and not a.no_fix_captions and not from_pk:
                # Parakeet captions are the benchmark (punctuated, on time): never let a rewrite touch them
                words = brain.fix_captions(brain_model, words, a.video_title or src.stem, a.vocab)
            tweet = a.title
            if a._spec.get("title") and (not tweet or tweet == "auto"):
                tweet = (r.get("_j") or {}).get("tweet") or (r.get("_j") or {}).get("title") or ""
            if tweet and a.channel and a.channel.lower() not in tweet.lower():
                tweet = f"{a.channel}: {tweet}"
            base = f"clip_{i:02d}_{hms(r['_c']['t'], '-')}"
            ass_name = None
            if (not a.no_captions or a.title) and build_ass(
                    words if not a.no_captions else [], s, out / f"{base}.ass", a.font, not a.no_upper,
                    a._spec.get("caption_y", 520), a.caption_style, a.caption_color, a._spec.get("caption"),
                    tweet if a._spec.get("title") else "", a.title_by, a._spec.get("title"), e - s,
                    "" if (not a.hook) else ((r.get("_j") or {}).get("hook") or ""), a._spec.get("hook")):
                ass_name = f"{base}.ass"
            render(src, info, s, e, ass_name, f"{base}.mp4", a, out, r["_c"]["t"] - s, str(r.get("category", "")))
            try:
                (out / f"{base}.edit.json").write_text(json.dumps(dict(
                    input=str(src), out=str(out), argv=sys.argv[1:], i=i, s=s, e=e, tweet=tweet, base=base,
                    peak_rel=r["_c"]["t"] - s, cat=str(r.get("category", "")), hook=(r.get("_j") or {}).get("hook") or "",
                    words=[dict(start=w["start"], end=w["end"], word=w["word"]) for w in words]), ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            r.update(file=f"{base}.mp4", start=hms(s), end=hms(e))
            if a._spec.get("title") and tweet:
                r["tweet"] = tweet
            log(f"  wrote {base}.mp4  ({e - s:.0f}s, {len(words)} caption words, took {time.time() - tc:.0f}s)")
        except Exception as ex:  # keep going: one bad clip should not stop the batch
            failed += 1
            log(f"  FAILED: {ex}")

    shutil.rmtree(tmp, ignore_errors=True)
    write_report(out, [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows])
    size = sum(f.stat().st_size for f in out.glob("*.mp4")) / 1e6
    log(f"\nDone: {len(rows) - failed} clips, {failed} failed, {size:.0f} MB of clips, total {time.time() - T0:.0f}s. "
        f"Open {out / 'index.html'} to review.")


if __name__ == "__main__":
    main()
