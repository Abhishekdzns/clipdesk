"""Live mode: watch a YouTube live stream as it happens and clip the moments at once.

  python live.py <live url> --name "Kai Cenat" --handle KaiCenat
  (or double-click run_live.bat, which reads live_job.json)

How it works
  1. yt-dlp finds the stream's address, ffmpeg records it into 10 second pieces (a rolling buffer, default 15 min).
  2. Every finished piece is scored second by second: chat speed (chat-downloader), loudness, laughing/screaming
     (YAMNet). A moment fires when at least two of those agree and the blended score is high.
  3. After the reaction ends (--post seconds) the buffer is cut around the moment and handed to clipper.py, which
     transcribes it, lets Claude (your subscription) judge it, and renders the tweet-layout clip.
  Clips land in runs/live/<name>_<time>/clips. Nothing is posted anywhere. The creators.json allowlist applies.
"""
from __future__ import annotations

import sys as _sys  # Windows pipes default to cp1252: a stray character in a log line must never stop a job
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LOGF = None


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    if LOGF:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------- args
def parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Clip a YouTube live stream while it is live (local, free).")
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--name", default="", help="streamer name, used in the clip header and for the AI")
    p.add_argument("--handle", default="", help="handle without @, used in the clip header")
    p.add_argument("--video-title", default="", help="stream title, context for the AI")
    p.add_argument("--brain-topic", default="", help="what the stream is about, context for the AI")
    p.add_argument("--brain-model", default="claude:sonnet")
    p.add_argument("--language", default="")
    p.add_argument("--layout", default="title_card")  # the N3ON look: facecam top, gameplay bottom
    p.add_argument("--facecam", default="", help="webcam box x:y:w:h for layouts that show the face")
    p.add_argument("--pre", type=float, default=25, help="seconds before the peak")
    p.add_argument("--post", type=float, default=12, help="seconds after the peak (also how long we wait)")
    p.add_argument("--min-len", type=float, default=20)
    p.add_argument("--max-len", type=float, default=45)
    p.add_argument("--seg", type=int, default=10, help="buffer piece length in seconds")
    p.add_argument("--buffer-min", type=float, default=15, help="minutes of stream kept on disk")
    p.add_argument("--warmup", type=float, default=180, help="seconds of watching before the first trigger")
    p.add_argument("--thr", type=float, default=4.0, help="blended score that fires a moment")
    p.add_argument("--min-active", type=int, default=2, help="how many of chat/loud/vocal must agree")
    p.add_argument("--cooldown", type=float, default=90, help="min seconds between two moments")
    p.add_argument("--chat-lag", type=float, default=8, help="seconds chat reacts after the stream shows something")
    p.add_argument("--max-clips-hour", type=int, default=8, help="cap on Claude-judged moments per hour")
    p.add_argument("--max-hours", type=float, default=8)
    p.add_argument("--catchup-min", type=float, default=60,
                   help="on start, also clip the last N minutes that already happened (0 = off)")
    p.add_argument("--catchup-top", type=int, default=3, help="most clips to render from the catch-up part")
    p.add_argument("--test-past", default="", help="treat this video file as the already-streamed part (testing)")
    p.add_argument("--bg-image", nargs="+", default=[str(HERE / "assets" / "bg_1.png"), str(HERE / "assets" / "bg_2.png")],
                   help="background picture(s) for the tweet layout, used in turn")
    p.add_argument("--no-chat", action="store_true")
    p.add_argument("--force-every", type=float, default=0, help="TEST ONLY: fire a moment every N seconds (replays have no chat)")
    p.add_argument("--force-at", default="", help="TEST ONLY: fire moments at these stream seconds, comma separated")
    p.add_argument("--no-vocal", action="store_true")
    p.add_argument("--keep-cuts", action="store_true", help="keep the raw cut of every moment")
    p.add_argument("--dry-cut", action="store_true", help="cut the moment out but do not run clipper.py")
    p.add_argument("--test-file", default="", help="pretend this video file is the live stream (testing)")
    p.add_argument("--out", default=str(HERE / "runs" / "live"))
    p.add_argument("--hinglish", action="store_true", help="Hindi stream: captions in Hinglish")
    p.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="extra args passed to clipper.py")
    a = p.parse_args()
    if not a.url and not a.test_file:
        job = HERE / "live_job.json"
        if not job.exists():
            sys.exit("Give a live URL, or fill in live_job.json.")
        cfg = json.loads(job.read_text(encoding="utf-8"))
        for k, v in cfg.items():
            # a run from a saved video (--test-past) gives its own name, topic, language and clip count on the command line:
            # live_job.json (an old live-test file with hinglish on) must not override them, only supply the placeholder url
            if not k.startswith("_") and not (a.test_past and k != "url"):
                setattr(a, k.replace("-", "_"), ([str(HERE / x) for x in v] if k.replace("-", "_") == "bg_image" else v))
    if not a.url and not a.test_file:
        sys.exit("No live URL given.")
    return a


# ---------------------------------------------------------------- capture
def stream_urls(url: str) -> list:
    import clipper
    r = subprocess.run(["yt-dlp", *clipper.yt_extra(), "-g", "--no-warnings", "-f", "bv*[vcodec^=avc1]+ba/bv*[vcodec^=avc1]/bv*+ba/b", url],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    urls = [x.strip() for x in r.stdout.splitlines() if x.strip().startswith("http")]
    if not urls:
        why = (r.stderr.strip().splitlines() or ["no details"])[-1][:300]
        raise RuntimeError(f"could not get the stream address (is it live right now?): {why}")
    return urls[:2]


class Capture(threading.Thread):
    """Runs ffmpeg into numbered ten second pieces. Restarts it if the stream address expires."""

    def __init__(self, a, buf: Path):
        super().__init__(daemon=True)
        self.a, self.buf, self.stop_ev, self.proc, self.ended = a, buf, threading.Event(), None, False
        self.next_idx = 0

    def cmd(self) -> list:
        a = self.a
        if a.test_file:
            ins = ["-re", "-i", a.test_file]
            maps = []
        else:
            urls = stream_urls(a.url)
            ins = sum((["-i", u] for u in urls), [])
            maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(urls) == 2 else []
        return (["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *ins, *maps, "-c", "copy", "-f", "segment",
                 "-segment_time", str(a.seg), "-segment_format", "mpegts", "-reset_timestamps", "1",
                 "-segment_start_number", str(self.next_idx), str(self.buf / "seg_%06d.ts")])

    def run(self) -> None:
        fails = 0
        while not self.stop_ev.is_set():
            t0 = time.time()
            try:
                c = self.cmd()
            except Exception as ex:  # noqa: BLE001
                log(f"capture: {ex}")
                fails += 1
                if fails >= 5:
                    break
                time.sleep(20)
                continue
            self.proc = subprocess.Popen(c, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
            err = self.proc.communicate()[1]
            segs = sorted(self.buf.glob("seg_*.ts"))
            if segs:
                self.next_idx = int(segs[-1].stem.split("_")[1]) + 1
            if self.stop_ev.is_set() or self.a.test_file:
                break
            fails = fails + 1 if time.time() - t0 < 60 else 0
            log(f"capture stopped ({(err or '').strip()[-150:]}), trying again" if fails < 5 else "stream seems to have ended")
            if fails >= 5:
                break
            time.sleep(10)
        self.ended = True

    def stop(self) -> None:
        self.stop_ev.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------------- chat
class Chat(threading.Thread):
    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url, self.stamps, self.ok, self.err = url, deque(maxlen=200000), False, ""

    def _own(self) -> None:
        """Own YouTube live chat reader (chat-downloader is broken by YouTube changes)."""
        import json as _j, urllib.request as _u
        hd = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en", "Cookie": "CONSENT=YES+1; SOCS=CAI"}
        html = _u.urlopen(_u.Request(self.url, headers=hd), timeout=30).read().decode("utf-8", "replace")
        m = re.search(r"ytInitialData\s*=\s*(\{.+?\});\s*</script>", html, re.S)
        data = _j.loads(m.group(1))
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)[1]
        ver = (re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html) or [0, "2.20260101.00.00"])[1]
        lc = data["contents"]["twoColumnWatchNextResults"]["conversationBar"]["liveChatRenderer"]
        cont = lc["continuations"][0]["reloadContinuationData"]["continuation"]
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}}}
        api = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false"
        self.ok = True
        while cont:
            body["continuation"] = cont
            r = _u.Request(api, data=_j.dumps(body).encode(), headers={**hd, "Content-Type": "application/json", "Origin": "https://www.youtube.com", "Referer": self.url})
            d = _j.loads(_u.urlopen(r, timeout=30).read().decode("utf-8", "replace"))
            c = d.get("continuationContents", {}).get("liveChatContinuation", {})
            for act in c.get("actions", []):
                item = act.get("addChatItemAction", {}).get("item", {})
                for k in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
                    if k in item:
                        ts = item[k].get("timestampUsec")
                        self.stamps.append(int(ts) / 1e6 if ts else time.time())
            cont, wait = None, 2.0
            for cc in c.get("continuations", []):
                for kk in ("invalidationContinuationData", "timedContinuationData"):
                    if kk in cc:
                        cont = cc[kk]["continuation"]
                        wait = min(max(cc[kk].get("timeoutMs", 2000) / 1000.0, 1.0), 6.0)
            time.sleep(wait)

    def run(self) -> None:
        try:
            self._own()
        except Exception as ex:  # noqa: BLE001
            self.ok, self.err = False, "own reader: " + str(ex)[:120]
        if self.ok and self.stamps:
            return
        try:
            from chat_downloader import ChatDownloader
            self.ok = True
            for m in ChatDownloader().get_chat(self.url, message_types=["text_message"]):
                ts = m.get("timestamp")
                self.stamps.append(ts / 1e6 if ts and ts > 1e12 else time.time())
        except Exception as ex:  # noqa: BLE001
            self.ok, self.err = False, self.err + " | " + str(ex)[:100]

    def counts(self, w0: float, n: int) -> np.ndarray:
        """messages per second for n seconds starting at wall time w0"""
        out = np.zeros(n, np.float32)
        for t in list(self.stamps):
            i = int(t - w0)
            if 0 <= i < n:
                out[i] += 1
        return out


# ---------------------------------------------------------------- scoring
def seg_info(path: Path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def seg_db(path: Path, n: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if len(x) < n * 100:
        return np.full(n, -80.0, np.float32)
    return np.array([20 * np.log10(np.sqrt(np.mean(b ** 2)) + 1e-6) for b in np.array_split(x, n)], np.float32)


def robust_z(x: np.ndarray, base: np.ndarray, floor: float) -> np.ndarray:
    if len(base) < 30:
        return np.zeros_like(x)
    med = np.median(base)
    mad = 1.4826 * np.median(np.abs(base - med))
    return (x - med) / max(mad, floor)


def box(x: np.ndarray, k: int) -> np.ndarray:
    return np.convolve(x, np.ones(k) / k, mode="same") if len(x) >= k else x


# ---------------------------------------------------------------- cutting and clipping
def cut_moment(segs: list, tp: float, a, buf: Path, outdir: Path, tag: str):
    """Join the buffer pieces around the peak into one mp4. Returns (path, peak seconds inside it) or None."""
    lo, hi = tp - a.pre - a.seg, tp + a.post + a.seg
    use = [s for s in segs if s["start"] + s["n"] >= lo and s["start"] <= hi and s["path"].exists()]
    if not use:
        return None
    first, rel = use[0], 0.0
    for s in use:
        if s["start"] <= tp < s["start"] + s["n"]:
            rel += (tp - s["start"]) * s["dur"] / s["n"]
            break
        rel += s["dur"]
    lst = outdir / f"{tag}.txt"
    lst.write_text("".join(f"file '{s['path'].resolve().as_posix()}'\n" for s in use), encoding="utf-8")
    out = outdir / f"{tag}.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                        "-bsf:a", "aac_adtstoasc", str(out)], capture_output=True, text=True)
    lst.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        log(f"cut failed: {(r.stderr or '')[-200:]}")
        return None
    return out, rel


PROCS: list = []


def move_side(m: Path, dest: Path, hq_off=None) -> None:
    """Keep the caption sidecar next to the clip so it can be edited and re-captioned later."""
    side = m.with_suffix(".edit.json")
    if not side.exists():
        return
    try:
        d = json.loads(side.read_text(encoding="utf-8"))
        d["out"], d["base"] = str(dest.parent), dest.stem
        dest.with_suffix(".edit.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:  # hooky title, title_card look, file named after the title, source-time info
        how = ["--hq", str(dest.with_suffix(".edit.json")), str(hq_off)] if hq_off is not None else ["--finish", str(dest.with_suffix(".edit.json"))]
        subprocess.run([sys.executable, "-u", "remaster.py", *how],
                       cwd=str(Path(__file__).resolve().parent), capture_output=True, timeout=900)
    except Exception:  # noqa: BLE001
        pass


def clipper_base(a) -> list:
    title_by = f"{a.name} @{a.handle}".strip() if (a.name or a.handle) else ""
    lay = a.layout
    try:
        _cr = json.loads((Path(__file__).resolve().parent / "creators.json").read_text(encoding="utf-8"))
        _c = next((c for c in _cr.get("creators", []) if a.name and c.get("name", "").lower() == a.name.lower()), {})
        lay = _c.get("layout") or lay
    except Exception:  # noqa: BLE001
        pass
    cmd = ["--layout", lay, "--min-len", str(a.min_len), "--max-len", str(a.max_len), "--pre", str(a.pre),
           "--post", str(a.post), "--brain-model", a.brain_model]
    if title_by:
        cmd += ["--title-by", title_by]
    if a.name:
        cmd += ["--channel", a.name]
    if a.video_title:
        cmd += ["--video-title", a.video_title]
    if a.brain_topic:
        cmd += ["--brain-topic", a.brain_topic]
    if a.language:
        cmd += ["--language", a.language]
    if getattr(a, "facecam", ""):
        cmd += ["--facecam", a.facecam]
    imgs = [x for x in getattr(a, "bg_images_now", a.bg_image) if Path(x).exists()]
    if imgs and a.layout == "tweet":
        cmd += ["--bg-image", *imgs]
    if getattr(a, "hinglish", False):
        cmd += ["--hinglish"]
    return cmd + list(a.extra)


def run_clipper_cmd(cmd: list, tag: str = "clipper") -> bool:
    """Run clipper.py, echo the lines that matter. Returns True when the Claude limit was hit."""
    p = subprocess.Popen([sys.executable, "-u", str(HERE / "clipper.py"), *cmd], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(HERE))
    PROCS.append(p)
    limit = False
    last = []
    for line in p.stdout:
        line = line.rstrip()
        if line.strip():
            last.append(line.strip()[:220])
            del last[:-25]
        if "CLAUDE LIMIT" in line or "usage_limit" in line:
            limit = True
        if line and any(k in line for k in ("KEEP", "skip ", "wrote ", "No moment passed", "Done:", "rror",
                                            "named", "Transcribing", "Picked", "scout:", "Parakeet", "Loading", "GPU:", "lines in", "CLAUDE LIMIT")):
            log(f"      {tag}: " + line.strip()[:200])
    rc = p.wait()
    if rc != 0:
        log(f"      {tag}: FAILED, clipper stopped with exit code {rc} (a negative or very large code usually means Windows ran out of memory)")
        for ln in last[-12:]:
            log(f"      {tag} last output: {ln}")
    return limit


def run_clipper(a, cut: Path, rel: float, clipdir: Path) -> tuple:
    cmd = [str(cut), "--out", str(clipdir), "--top", "1", "--at", f"{rel:.1f}", *([] if getattr(a, "force_at", "") else ["--strict"]), "--no-face",
           *([] if getattr(a, "hinglish", False) else ["--no-fix-captions"]), *clipper_base(a)]
    limit = run_clipper_cmd(cmd)
    return sorted(clipdir.glob("clip_*.mp4")), limit


# ---------------------------------------------------------------- catch-up (what already happened)
def stream_start(url: str):
    import clipper
    r = subprocess.run(["yt-dlp", *clipper.yt_extra(), "--skip-download", "--no-warnings", "--print",
                        "%(release_timestamp)s|%(timestamp)s", url],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    for x in (r.stdout.strip().splitlines() or [""])[-1].split("|"):
        try:
            v = float(x)
            if v > 1e9:
                return v
        except ValueError:
            pass
    return None


def catchup(a, run_dir: Path, stop_ev: threading.Event, made: list) -> None:
    """Clip the part of the stream that happened before we joined: download the last N minutes from the start of
    the live buffer, then run the normal full pass (Whisper, Claude scout and judge, render all keepers)."""
    cdir = run_dir / "catchup"
    cdir.mkdir(exist_ok=True)
    try:
        if a.test_past:
            past, off = Path(a.test_past), 0.0
        else:
            import clipper
            start = stream_start(a.url)
            if not start:
                log("catch-up: YouTube does not publish when this stream started, so it is skipped")
                return
            elapsed = time.time() - start
            if elapsed < 150:
                log("catch-up: the stream only just started, nothing to catch up on")
                return
            off = max(0.0, elapsed - a.catchup_min * 60)
            hi = max(off + 30, elapsed - 20)
            log(f"catch-up: stream is {int(elapsed // 60)} min old, fetching minutes "
                f"{int(off // 60)} to {int(hi // 60)} of it (runs next to the live watching)")
            cmd = ["yt-dlp", *clipper.yt_extra(), "--download-sections", f"*{int(off)}-{int(hi)}",
                   "-f", "bv*[vcodec^=avc1]+ba/bv*[vcodec^=avc1]/bv*+ba/b", "--merge-output-format", "mp4", "--retries", "10",
                   "--fragment-retries", "30", "-o", str(cdir / "past.%(ext)s"), a.url]
            try:
                (cdir / "offset.txt").write_text(str(int(off)), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            PROCS.append(p)
            t0, last = time.time(), ""
            for line in p.stdout:
                line = line.strip()
                if line.startswith("ERROR") or "rror" in line[:12]:
                    last = line[:250]
                if stop_ev.is_set() or time.time() - t0 > 3000:
                    p.terminate()
                    break
            p.wait()
            found = sorted(cdir.glob("past*.*"), key=lambda q: q.stat().st_size, reverse=True)
            found = [f for f in found if f.suffix.lower() in (".mp4", ".mkv", ".webm")]
            if not found:
                log(f"catch-up: could not fetch the earlier part ({last or 'yt-dlp gave no file'}). Live watching goes on.")
                return
            past = found[0]
        log(f"catch-up: got {past.name} ({past.stat().st_size // 1_000_000} MB), reading it now")
        out = cdir / "work"
        limit = run_clipper_cmd([str(past), "--out", str(out), "--top", str(a.catchup_top), "--all-keepers",
                                 "--strict", *clipper_base(a)], "catch-up")
        if limit:
            log("catch-up: Claude limit hit, the catch-up pass stopped early")
        n = 0
        for m in sorted(out.glob("clip_*.mp4")):
            mm = re.search(r"(\d+)-(\d+)-(\d+)", m.stem)
            when = ""
            if mm:
                sec = int(mm[1]) * 3600 + int(mm[2]) * 60 + int(mm[3]) + off
                when = f"_at{int(sec // 3600):01d}h{int(sec % 3600 // 60):02d}m"
            dest = run_dir / "clips" / f"catchup{when}_{m.name}"
            shutil.move(str(m), dest)
            move_side(m, dest)
            made.append(dest)
            n += 1
            log(f"CATCH-UP CLIP READY: {dest}")
        log(f"catch-up done: {n} clip(s)")
    except Exception as ex:  # noqa: BLE001
        log(f"catch-up failed: {str(ex)[:200]}. Live watching goes on.")


def _dl_chunk(url, a0, b0, dest: Path, log_tag: str) -> bool:
    """480p reading copy of one stretch of the stream. Retries, and restarts a download that stops growing."""
    import clipper
    for attempt in range(3):
        for old in dest.parent.glob(dest.stem + ".*"):
            old.unlink(missing_ok=True)
        cmd = ["yt-dlp", *clipper.yt_extra(), "-N", "4", "--download-sections", f"*{int(a0)}-{int(b0)}",
               "-f", "bv*[height<=480][vcodec^=avc1]+ba/bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4",
               "--retries", "10", "--fragment-retries", "30", "-o", str(dest.with_suffix(".%(ext)s")), url]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PROCS.append(p)
        last, t_last = -1, time.time()
        while p.poll() is None:
            time.sleep(10)
            size = sum(f.stat().st_size for f in dest.parent.glob(dest.stem + "*") if f.is_file())
            if size != last:
                last, t_last = size, time.time()
            elif time.time() - t_last > 180:
                log(f"catch-up: {log_tag} stalled for 3 min, restarting it ({attempt + 1}/3)")
                p.terminate()
                break
        if dest.exists() and dest.stat().st_size > 100_000:
            return True
    return False


def catchup_full(a, run_dir: Path, stop_ev: threading.Event, made: list) -> None:
    """Everything the stream showed before we joined: read in 30-min chunks, newest first, 4 downloads at a time,
    at 480p (fast). Each picked moment is then remade from a 1080p window (CLIP RULES: never ship 480p)."""
    cdir = run_dir / "catchup"
    cdir.mkdir(exist_ok=True)
    try:
        start = stream_start(a.url)
        if not start:
            log("catch-up: YouTube does not say when this stream started, falling back to the last hour")
            return catchup(a, run_dir, stop_ev, made)
        upto = time.time() - start - 30
        if upto < 150:
            log("catch-up: the stream only just started, nothing to catch up on")
            return
        step = 1800
        chunks = []
        b = upto
        while b > 0:
            chunks.append((max(0, b - step), b))
            b -= step
        log(f"catch-up: stream is {int(upto // 60)} min old, reading all of it in {len(chunks)} chunk(s), newest first")
        (cdir / "offset.txt").write_text("0", encoding="utf-8")
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=4)
        futs = [(c, pool.submit(_dl_chunk, a.url, c[0], c[1], cdir / f"past_{int(c[0])}.mp4", f"minutes {int(c[0] // 60)}-{int(c[1] // 60)}"))
                for c in chunks]
        seen = []
        for (a0, b0), fu in futs:
            if stop_ev.is_set():
                break
            if not fu.result():
                log(f"catch-up: could not get minutes {int(a0 // 60)}-{int(b0 // 60)}, skipped (the after-stream sweep gets it)")
                continue
            past = cdir / f"past_{int(a0)}.mp4"
            out = cdir / f"work_{int(a0)}"
            log(f"catch-up: reading minutes {int(a0 // 60)}-{int(b0 // 60)}")
            limit = run_clipper_cmd([str(past), "--out", str(out), "--top", str(a.catchup_top), "--all-keepers", "--strict", *clipper_base(a)],
                                    f"catch-up {int(a0 // 60)}m")
            for m in sorted(out.glob("clip_*.mp4")):
                mm = re.search(r"(\d+)-(\d+)-(\d+)", m.stem)
                sec = (int(mm[1]) * 3600 + int(mm[2]) * 60 + int(mm[3]) + a0) if mm else a0
                if any(abs(sec - x) < 40 for x in seen):
                    continue  # same moment as a clip already made
                seen.append(sec)
                dest = run_dir / "clips" / f"catchup_at{int(sec // 3600)}h{int(sec % 3600 // 60):02d}m_{m.name}"
                shutil.move(str(m), dest)
                move_side(m, dest, hq_off=a0)
                made.append(dest)
                log(f"CATCH-UP CLIP READY: {dest}")
            if limit:
                log("catch-up: Claude limit hit, stopping the catch-up here")
                break
        pool.shutdown(wait=False, cancel_futures=True)
        log(f"catch-up done: {len(made)} clip(s)")
    except Exception as ex:  # noqa: BLE001
        log(f"catch-up failed: {str(ex)[:200]}. Live watching goes on.")


class Worker(threading.Thread):
    def __init__(self, a, run_dir: Path):
        super().__init__(daemon=True)
        self.a, self.run_dir, self.q = a, run_dir, queue.Queue()
        self.paused_until, self.times, self.made, self.n = 0.0, deque(), [], 0

    def budget_ok(self) -> bool:
        now = time.time()
        while self.times and now - self.times[0] > 3600:
            self.times.popleft()
        return len(self.times) < self.a.max_clips_hour and now >= self.paused_until

    def run(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                break
            cut, rel, tag = job
            if self.a.dry_cut:
                log(f"  [{tag}] dry cut saved: {cut.name} (peak at {rel:.1f}s inside it)")
                continue
            if not self.budget_ok():
                log(f"  [{tag}] Claude budget or limit pause: cut kept unjudged in cuts/{cut.name}")
                continue
            self.times.append(time.time())
            self.n += 1
            k = (self.n - 1) % max(1, len(self.a.bg_image))
            self.a.bg_images_now = self.a.bg_image[k:] + self.a.bg_image[:k]
            clipdir = self.run_dir / "clips" / tag
            t0 = time.time()
            try:
                made, limit = run_clipper(self.a, cut, rel, clipdir)
            except Exception as ex:  # noqa: BLE001
                log(f"  [{tag}] clipper failed: {str(ex)[:200]}")
                continue
            if limit:
                self.paused_until = time.time() + 1800
                log(f"  [{tag}] Claude limit hit, no judging for 30 min. Cuts stay in cuts/ to judge later.")
                continue
            if made:
                for m in made:
                    dest = self.run_dir / "clips" / f"{tag}_{m.name}"
                    shutil.move(str(m), dest)
                    move_side(m, dest)
                    self.made.append(dest)
                    log(f"  [{tag}] CLIP READY ({time.time() - t0:.0f}s): {dest}")
                shutil.rmtree(clipdir, ignore_errors=True)
                if not self.a.keep_cuts:
                    cut.unlink(missing_ok=True)
            else:
                log(f"  [{tag}] judge said no clip here ({time.time() - t0:.0f}s)")
                shutil.rmtree(clipdir, ignore_errors=True)
                if not self.a.keep_cuts:
                    cut.unlink(missing_ok=True)


# ---------------------------------------------------------------- main loop
def main() -> None:
    global LOGF
    a = parse()
    if not a.test_file:
        import clipper
        try:
            clipper.check_source(a.url)
        except SystemExit as ex:
            sys.exit(str(ex))
    if not a.test_file and not (a.name and a.handle and a.video_title):
        try:
            import clipper
            r = subprocess.run(["yt-dlp", *clipper.yt_extra(), "--skip-download", "--no-warnings", "--print",
                                "%(channel)s|%(uploader_id)s|%(title)s", a.url], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            ch, up, ti = ((r.stdout.strip().splitlines() or [""])[-1].split("|") + ["", "", ""])[:3]
            a.name = a.name or ("" if ch == "NA" else ch)
            a.handle = a.handle or ("" if up == "NA" else up.lstrip("@"))
            a.video_title = a.video_title or ("" if ti == "NA" else ti)
        except Exception:  # noqa: BLE001
            pass
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = Path(a.out) / f"{re.sub(r'[^A-Za-z0-9]+', '_', a.name or 'stream')}_{stamp}"
    buf, cuts = run_dir / "buffer", run_dir / "cuts"
    for d in (buf, cuts, run_dir / "clips"):
        d.mkdir(parents=True, exist_ok=True)
    LOGF = run_dir / "live_log.txt"
    log(f"Live mode on {a.url or a.test_file}")
    log(f"Run folder: {run_dir}")

    use_vocal = not a.no_vocal
    ve_fn = None
    if use_vocal:
        try:
            import av_signals
            ok, why = av_signals.available()
            if ok:
                av_signals.model("yamnet.tflite")
                ve_fn = av_signals.vocal_events
            else:
                log(f"laugh/scream detector off: {why}")
        except Exception as ex:  # noqa: BLE001
            log(f"laugh/scream detector off: {str(ex)[:120]}")
    chat = None
    if not a.no_chat and not a.test_file:
        chat = Chat(a.url)
        chat.start()
        time.sleep(4)
        log("chat: " + ("listening" if chat.ok else f"off ({chat.err or 'pip install chat-downloader'})"))
    min_active = a.min_active

    cap = Capture(a, buf)
    cap.start()
    worker = Worker(a, run_dir)
    worker.start()
    stop_ev = threading.Event()
    catch_made: list = []
    if a.catchup_min > 0 and a.url and not a.test_file and not a.test_past:
        threading.Thread(target=catchup_full, args=(a, run_dir, stop_ev, catch_made), daemon=True).start()
    elif a.test_past:
        threading.Thread(target=catchup, args=(a, run_dir, stop_ev, catch_made), daemon=True).start()

    segs: list = []          # finished pieces: path, start (s on our time axis), n (s), dur (real s), wall
    db_all, za_all, zc_all, v_all = [], [], [], []
    seen_idx, pending, last_trig, n_moments = set(), [], -1e9, 0
    t_begin = time.time()
    done_wall = []           # wall time each piece was first seen finished
    log("Watching. Warm-up first, so the first moments can only fire after "
        f"{int(a.warmup)}s. Press Ctrl+C (or close the window) to stop.")
    try:
        while time.time() - t_begin < a.max_hours * 3600:
            time.sleep(1.0)
            files = sorted(buf.glob("seg_*.ts"))
            idxs = [int(f.stem.split("_")[1]) for f in files]
            # a piece is finished once the next one exists (or the capture ended)
            fin = files[:-1] if not cap.ended else files
            for f in fin:
                i = int(f.stem.split("_")[1])
                if i in seen_idx:
                    continue
                seen_idx.add(i)
                dur = seg_info(f)
                if dur < 1:
                    continue
                n = max(1, int(round(dur)))
                start = len(db_all)
                wall_fin = time.time()
                db = seg_db(f, n)
                db_all += list(db)
                base = np.array(db_all[-900:-n] if len(db_all) > n else [], np.float32)
                za = box(robust_z(db, base, 2.5), 3)
                za_all += list(za)
                if ve_fn is not None:
                    try:
                        cp = buf / f"{f.stem}_v.npz"
                        ve = ve_fn(f, dur, cp, lambda *_: None)
                        cp.unlink(missing_ok=True)
                        voc = np.zeros(n, np.float32)
                        v = np.maximum(ve["laugh"], ve["scream"])[:n]
                        voc[:len(v)] = v
                    except Exception as ex:  # noqa: BLE001
                        log(f"laugh/scream detector failed once, switching it off: {str(ex)[:120]}")
                        ve_fn, voc = None, np.zeros(n, np.float32)
                else:
                    voc = np.zeros(n, np.float32)
                v_all += list(voc)
                segs.append(dict(path=f, start=start, n=n, dur=dur, wall=wall_fin))
                done_wall.append((start, n, wall_fin))
            # score pieces once chat has had time to answer
            for s in segs:
                if s.get("scored") or time.time() < s["wall"] + a.chat_lag + 2:
                    continue
                s["scored"] = True
                st, n = s["start"], s["n"]
                za = np.array(za_all[st:st + n])
                voc = np.array(v_all[st:st + n])
                if chat is not None and chat.ok:
                    w0 = s["wall"] - n + a.chat_lag
                    cnt = box(chat.counts(w0, n), 5) * 5  # messages per 5 s
                    zc = robust_z(cnt, np.array(chat_base_list[-900:], np.float32), 1.5)
                    chat_base_list.extend(list(cnt))
                else:
                    zc = np.zeros(n, np.float32)
                zc_all += list(zc)
                score = np.maximum(zc, 0) * 1.0 + np.maximum(za, 0) * 0.6 + voc * 3.0
                active = (zc >= 2.5).astype(int) + (za >= 2.5).astype(int) + (voc >= 0.4).astype(int)
                hit = np.where((score >= a.thr) & (active >= min_active))[0]
                tnow = st + (int(hit[np.argmax(score[hit])]) if len(hit) else 0)
                log(f"piece {int(s['path'].stem.split('_')[1]):4d} t={st // 60:02d}:{st % 60:02d} "
                    f"chat={float(zc.max()):4.1f} loud={float(za.max()):4.1f} vocal={float(voc.max()):3.1f} "
                    f"score={float(score.max()):4.1f}" + ("  <== MOMENT" if len(hit) else ""))
                fat = [float(x) for x in a.force_at.split(",") if x.strip()] if a.force_at else []
                if a.test_file and any(st <= x < st + n for x in fat):
                    x_ = next(x for x in fat if st <= x < st + n)
                    hit, tnow = np.array([int(x_ - st)]), int(x_)
                    log("TEST: forced moment at a known good spot (replay has no chat)")
                elif a.force_every and a.test_file and st >= a.warmup and st - last_trig >= a.force_every:
                    hit, tnow = np.array([int(np.argmax(score))]), st + int(np.argmax(score))
                    log("TEST: forced moment (replay has no chat)")
                if len(hit) and st >= a.warmup and tnow - last_trig >= a.cooldown:
                    last_trig = tnow
                    n_moments += 1
                    pending.append(dict(tp=float(tnow), due=tnow + a.post + 3,
                                        tag=f"m{n_moments:03d}_{tnow // 60:02d}m{tnow % 60:02d}s"))
                    log(f"MOMENT at {tnow // 60:02d}:{tnow % 60:02d} stream time, cutting in {int(a.post)}s")
            # cut moments whose reaction time has passed
            end_t = segs[-1]["start"] + segs[-1]["n"] if segs else 0
            for pnd in list(pending):
                if pnd["due"] <= end_t:
                    pending.remove(pnd)
                    r = cut_moment(segs, pnd["tp"], a, buf, cuts, pnd["tag"])
                    if r:
                        worker.q.put((r[0], r[1], pnd["tag"]))
                        log(f"  [{pnd['tag']}] cut ready, sent to judge and render")
            # trim the rolling buffer
            keep_from = end_t - a.buffer_min * 60
            pend_lo = min([p_["tp"] - a.pre - a.seg for p_ in pending], default=1e18)
            for s in list(segs):
                if s["start"] + s["n"] < min(keep_from, pend_lo) and s["path"].exists():
                    s["path"].unlink(missing_ok=True)
            if cap.ended and time.time() - max([s["wall"] for s in segs], default=0) > 30:
                log("stream ended")
                if a.url and not a.test_file:
                    try:
                        todo = HERE / "sweep_todo.json"
                        lst = json.loads(todo.read_text(encoding="utf-8")) if todo.exists() else []
                        lst.append(dict(name=a.name, url=a.url, run=str(run_dir), ended=int(time.time())))
                        todo.write_text(json.dumps(lst, indent=1), encoding="utf-8")
                        log("after-stream sweep queued: the full video is read once YouTube has processed it")
                    except Exception:  # noqa: BLE001
                        pass
                break
    except KeyboardInterrupt:
        log("stopping...")
    finally:
        cap.stop()
        stop_ev.set()
        for p_ in PROCS:
            if p_.poll() is None and p_ is not None and getattr(p_, "args", [None])[0] == "yt-dlp":
                p_.terminate()
        worker.q.put(None)
        if worker.is_alive():
            log("finishing the clip that is being made...")
            worker.join(timeout=900)
        shutil.rmtree(buf, ignore_errors=True)
        log(f"Done. {len(worker.made)} live clip(s) and {len(catch_made)} catch-up clip(s) in {run_dir / 'clips'}")


chat_base_list: list = []

if __name__ == "__main__":
    main()
