"""Jobs view for Clipdesk: every clipping run in runs/live with its mode (live or catch-up), phase and live progress,
plus starting a new run from a link. Everything is read from the run folders and logs, so runs started any other way
(a .vbs, the command line) show up too. Used by server.py: build(paused) and start(body, creators_file, yt_extra, event)."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "runs" / "live"
RUN_LOG = HERE / "run_streamer_log.txt"
_PREV = {}      # run name -> (time, bytes, speed)
_BUSY = set()   # runs whose metadata is being fetched
_YTX = [lambda: []]


def _pids(vid: str, name: str = "", kids: bool = True) -> list:
    """Process ids of a run: run_streamer for this video, plus (kids) its clipper, yt-dlp and ffmpeg children."""
    if sys.platform != "win32" or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid or ""):
        return []
    names = "'python.exe','pythonw.exe'" + (",'ffmpeg.exe','yt-dlp.exe'" if kids else "")
    like = f"$_.CommandLine -like '*{vid}*'" + (f" -or $_.CommandLine -like '*{name}*'" if (kids and name) else "")
    ps = f"Get-CimInstance Win32_Process | Where-Object {{ ($_.Name -in {names}) -and ({like}) }} | Select-Object -ExpandProperty ProcessId"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=25,
                           creationflags=0x08000000)
    except Exception:  # noqa: BLE001
        return []
    return [int(x) for x in r.stdout.split() if x.strip().isdigit()]


def _claude_wait() -> str:
    """Message while brain.py is waiting for the Claude limit to reset (it refreshes claude_wait.json every minute)."""
    try:
        d = json.loads(_read(HERE / "claude_wait.json"))
        if time.time() - float(d.get("ts", 0)) < 300:
            return str(d.get("msg", "Waiting for the Claude limit to reset"))
    except (ValueError, TypeError):
        pass
    return ""


def _per_moment() -> float:
    """Seconds Claude takes per judged moment on this PC, from the real calls logged in claude_usage.jsonl
    (time between neighbouring calls in a burst, which includes cutting frames). Falls back to 10 s."""
    try:
        ts = []
        for l in _read(HERE / "claude_usage.jsonl").splitlines()[-200:]:
            try:
                ts.append(json.loads(l)["t"])
            except (ValueError, KeyError):
                pass
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if 2 <= b - a < 60)
        if len(gaps) >= 8:
            return max(5.0, float(gaps[len(gaps) // 2]))
    except Exception:  # noqa: BLE001
        pass
    return 10.0


def _scout_secs() -> float:
    """Seconds per scout call (Claude reading ~10 minutes of transcript): the longer prompts in claude_usage.jsonl."""
    try:
        v = []
        for l in _read(HERE / "claude_usage.jsonl").splitlines()[-300:]:
            try:
                r = json.loads(l)
                if r.get("prompt_chars", 0) >= 4000 and r.get("secs"):
                    v.append(float(r["secs"]))
            except (ValueError, KeyError):
                pass
        if len(v) >= 5:
            v.sort()
            return max(6.0, v[len(v) // 2])
    except Exception:  # noqa: BLE001
        pass
    return 12.0


def _scout_prog(live_log: str):
    """(parts done, parts total, seconds per part measured on this run or None) from the 'scout: A of B parts' lines."""
    pts = []
    for ln in live_log.splitlines():
        m = re.match(r"(\d\d):(\d\d):(\d\d)\s.*scout: (\d+) of (\d+) parts", ln)
        if m:
            pts.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), int(m.group(4)), int(m.group(5))))
    if not pts:
        return None
    t, k, n = pts[-1]
    per = ((t - pts[0][0]) % 86400) / (k - pts[0][1]) if k - pts[0][1] >= 3 and (t - pts[0][0]) % 86400 > 0 else None
    return k - 1, n, per   # the line is written as part k starts, so k-1 are finished


def _live_rate(live_log: str):
    """Real judging speed of THIS run: judged moments per second, from the times on the KEEP/skip lines."""
    pts = []
    for l in live_log.splitlines():
        m = re.match(r"(\d\d):(\d\d):(\d\d)\s.*\b(?:KEEP|skip)\b", l)
        if m:
            pts.append(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)))
    if len(pts) >= 5 and (pts[-1] - pts[0]) % 86400 > 0:
        return (len(pts) - 1) / ((pts[-1] - pts[0]) % 86400)
    return None


def _nm(run: Path) -> str:
    return re.sub(r"_\d{8}_\d{6}$", "", run.name).replace("_", " ")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _url(run: Path) -> str:
    m = re.search(r"https?://\S+", _read(run / "live_log.txt")[:400])
    return m.group(0) if m else ""


def _vid(url: str) -> str:
    m = re.search(r"(?:v=|/live/|youtu\.be/|/shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else ""


def _started(run: Path) -> float:
    m = re.search(r"_(\d{8})_(\d{6})$", run.name)
    if not m:
        return run.stat().st_ctime
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except ValueError:
        return run.stat().st_ctime


# ---------------------------------------------------------------- metadata (title, length, mode, expected size)
def _fetch_meta(run: Path, url: str, ytx) -> None:
    try:
        r = subprocess.run(["yt-dlp", *ytx, "-J", "--skip-download", "--no-warnings", url], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        j = json.loads(r.stdout)
        dur = float(j.get("duration") or 0)
        best_v, best_a = None, None
        for f in j.get("formats", []):
            if f.get("vcodec", "none") != "none" and f.get("acodec", "none") == "none":
                if (f.get("height") or 9999) <= 480 and str(f.get("vcodec", "")).startswith("avc1"):
                    if not best_v or (f.get("height") or 0, f.get("tbr") or 0) > (best_v.get("height") or 0, best_v.get("tbr") or 0):
                        best_v = f
            elif f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none":
                if not best_a or str(f.get("format_id")) == "140":
                    best_a = f

        def size(f):
            if not f:
                return 0
            return float(f.get("filesize") or f.get("filesize_approx") or (f.get("tbr") or 0) * 125 * dur)
        meta = {"title": j.get("title", ""), "duration": dur, "live_status": j.get("live_status", ""),
                "channel": j.get("channel", ""), "vsize": size(best_v), "asize": size(best_a)}
        (run / "job_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    finally:
        _BUSY.discard(run.name)


def _meta(run: Path, url: str) -> dict:
    p = run / "job_meta.json"
    if p.exists():
        try:
            return json.loads(_read(p))
        except ValueError:
            return {}
    if url and run.name not in _BUSY and (run / "catchup").exists():
        _BUSY.add(run.name)
        try:
            ytx = _YTX[0]()
        except Exception:  # noqa: BLE001
            ytx = []
        threading.Thread(target=_fetch_meta, args=(run, url, ytx), daemon=True).start()
    return {}


# ---------------------------------------------------------------- one run
def _mode(meta: dict) -> str:
    ls = (meta.get("live_status") or "").lower()
    if ls == "is_live":
        return "Live mode (reads what has happened so far)"
    if ls == "was_live":
        return "Catch-up (stream has ended)"
    if ls == "not_live":
        return "Catch-up (video)"
    return "Catch-up"


def _run_log_lines(name: str) -> list:
    lines = [l for l in _read(RUN_LOG).splitlines() if re.match(r"\d\d:\d\d:\d\d ", l)]
    start = -1
    for i, l in enumerate(lines):
        if l[9:].startswith(f"== {name}: start"):
            start = i
    return [l[9:] for l in lines[start + 1:] if l[9:].startswith(name + ":") or l[9:].startswith(f"== {name}")] if start >= 0 else []


def job(run: Path, now: float, paused: set) -> dict:
    name = _nm(run)
    url = _url(run)
    meta = _meta(run, url)
    cd = run / "catchup"
    work = cd / "work"
    live_log = _read(run / "live_log.txt")
    log_lines = [l for l in live_log.splitlines() if l.strip()]
    rl = _run_log_lines(name)
    rl_text = "\n".join(rl)
    clips = sorted((run / "clips").glob("*.mp4")) if (run / "clips").exists() else []
    first_pass = len(list(work.glob("clip_*.mp4"))) if work.exists() else 0
    m = re.search(r"first pass made (\d+) clips", rl_text)
    made = int(m.group(1)) if m else 0

    parts = []
    if cd.exists():
        for f in cd.iterdir():
            if f.is_file() and "Frag" not in f.name and f.suffix not in (".ytdl", ".txt", ".json") \
                    and (re.match(r"past\.f\d+\.", f.name) or f.name.startswith("audio.")):
                parts.append(f)
    past = cd / "past.mp4"
    have_past = past.exists()
    dl_bytes = sum(f.stat().st_size for f in parts)
    total = float(meta.get("vsize", 0)) + float(meta.get("asize", 0))
    allf = [f for f in cd.iterdir() if f.is_file()] if cd.exists() else []
    newest = max([f.stat().st_mtime for f in allf] + [0])
    merging = any("temp" in f.name.lower() or f.suffix == ".ytdl" for f in allf) or \
        (parts and not any(f.suffix == ".part" for f in parts))

    speed = 0.0
    if parts:
        pv = _PREV.get(run.name)
        if pv and now - pv[0] >= 4:
            speed = max(0.0, (dl_bytes - pv[1]) / (now - pv[0]))
            _PREV[run.name] = (now, dl_bytes, speed)
        elif pv:
            speed = pv[2]
        else:
            _PREV[run.name] = (now, dl_bytes, 0.0)

    state, phase, pct, detail = "running", "Starting", None, ""
    dl_eta = tr_rate = tr_left = None
    judged = len(re.findall(r"\b(?:KEEP|skip)\b", live_log))
    kept = len(re.findall(r"\bKEEP\b", live_log))
    named = re.search(r"named (\d+) moment", live_log)
    picked = re.search(r"Picked (\d+) moments", live_log)
    failed = re.search(r"FAILED|download failed|STOP:", rl_text)

    if failed:
        state, phase = "error", "Failed"
        detail = (re.findall(r"(?:FAILED|download failed|STOP:).*", rl_text) or [""])[-1][:200]
    elif re.search(r"== .*: DONE", rl_text):
        state, phase, pct = "done", "Done", 1.0
        detail = f"{len(clips)} clip(s) ready"
        if not clips and not (work.exists() and list(work.glob("transcript_*.json"))):
            state, phase, pct = "error", "Stopped early", None
            detail = "the reader stopped before finishing the transcript, so 0 clips were made (it did not finish normally)"
    elif "re-making every clip" in rl_text:
        phase = "Rendering clips at 1080p"
        pct = (len(clips) / made) if made else None
        detail = f"{len(clips)} of {made} clips rendered"
    elif have_past and not parts and work.exists() and list(work.glob("transcript_*.json")):
        if not named:
            phase, detail = "Claude is reading the transcript", "finding moments"
            sp = _scout_prog(live_log)
            if sp and sp[1]:
                pct = min(0.99, sp[0] / sp[1])
                detail = f"Claude is reading the transcript: {sp[0]} of {sp[1]} parts done"
        else:
            phase = "Picking moments"
            detail = f"{judged} judged, {kept} kept (Claude named {named.group(1)}, {picked.group(1) if picked else named.group(1)} to judge)"
            tot = int(picked.group(1)) if picked else int(named.group(1))
            pct = min(0.99, judged / tot) if tot else None
    elif have_past and not parts and not ((work / "_tmp" / "whole.wav").exists() or (work / "transcribe_progress.json").exists()):
        phase = "Analysing sound and faces"
        detail = "first step of reading the video (about 7 min for a long stream), transcribing starts next"
        _s = re.findall(r"sounds: (\d+) / (\d+) min", live_log)
        _f = re.findall(r"facecam: (\d+) / (\d+) min", live_log)
        _sf = 1.0 if "Sound events done" in live_log else (min(1.0, int(_s[-1][0]) / max(1, int(_s[-1][1]))) if _s else 0.0)
        _ff = 1.0 if "Face reactions done" in live_log or "no face found" in live_log or "skipping face" in live_log else (min(1.0, int(_f[-1][0]) / max(1, int(_f[-1][1]))) if _f else 0.0)
        if _s or _f or _sf or _ff:
            pct = min(0.99, (_sf + _ff) / 2)
            detail = f"sound {int(_sf * 100)}%, faces {int(_ff * 100)}%. Transcribing starts next"
    elif have_past and not parts:
        phase = "Transcribing"
        since = int(now - (work / "_tmp" / "whole.wav").stat().st_mtime) if (work / "_tmp" / "whole.wav").exists() \
            else int(now - past.stat().st_mtime)
        detail = f"speech to text, running {since // 60} min"
        pf = work / "transcribe_progress.json"
        try:
            pj = json.loads(_read(pf)) if pf.exists() and now - pf.stat().st_mtime < 180 else None
        except ValueError:
            pj = None
        if pj and pj.get("total"):
            pct = min(0.99, pj["done"] / pj["total"])
            el = max(1.0, pj["t"] - pj["t0"])
            rate = pj["done"] / el if pj["done"] > 0 else 0
            eta = (pj["total"] - pj["done"]) / rate if rate > 0 else None
            tr_rate, tr_left = (rate or None), eta
            detail = (f"speech to text: {int(pj['done'] // 3600)}h {int(pj['done'] % 3600 // 60)}m of "
                      f"{int(pj['total'] // 3600)}h {int(pj['total'] % 3600 // 60)}m of audio"
                      + (f", about {int(eta // 60)} min left" if eta else ""))
        else:
            detail += " (this run has no exact percentage, it started before progress was added)"
    elif have_past and parts:
        phase, detail = "Merging video and audio", ""
    elif parts and merging and now - newest < 900:
        phase, detail = "Merging video and audio", "joining the downloaded pieces into one file"
    elif parts:
        age = now - newest
        phase = "Downloading"
        pct = min(0.99, dl_bytes / total) if total else None
        eta = (total - dl_bytes) / speed if total and speed > 0 else None
        dl_eta = eta
        detail = f"{dl_bytes / 1e9:.2f} GB" + (f" of about {total / 1e9:.2f} GB" if total else "") \
            + (f", {speed / 1e6:.1f} MB/s" if speed else "") + (f", about {int(eta // 60)} min left" if eta else "")
        if run.name in paused:
            state, phase = "paused", "Download paused"
        elif age > 120:
            state, phase = "stalled", "Download stalled"
            detail += f" (no data for {int(age)} s)"
    else:
        age = now - _started(run)
        if age > 300:
            state, phase, detail = "stalled", "Not started", "no files or log lines after 5 minutes"

    halt = False
    cw = _claude_wait()
    paused_at = _read(run / "paused.txt").strip() if (run / "paused.txt").exists() else ""
    pause_req = _read(run / "pause_req.txt").strip() if (run / "pause_req.txt").exists() else ""
    if paused_at and state in ("running", "warn", "stalled") and not _pids(_vid(url), name, kids=False):
        state, detail = "paused", f"paused after {paused_at}. Press Resume to continue from the saved files"
    elif (run / "stopped.txt").exists() and state in ("running", "warn", "stalled") and clips and not _pids(_vid(url), name, kids=False):
        state, phase, pct, detail = "done", "Done", 1.0, f"{len(clips)} clip(s) ready"  # clips exist: a later stop does not make the run an error
    elif (run / "stopped.txt").exists() and state in ("running", "warn", "stalled"):
        state, phase, pct, detail, halt = "error", "Stopped by you", None, "you stopped this job; run it again from saved files to continue", True
    elif state == "running" and have_past and not cw and phase != "Downloading":
        acts = [(run / "live_log.txt").stat().st_mtime if (run / "live_log.txt").exists() else 0]
        if work.exists():
            acts += [f.stat().st_mtime for f in work.iterdir() if f.is_file()]
        quiet = now - max(acts + [pst_ if (pst_ := past.stat().st_mtime) else 0])
        if quiet > 1800 and not _pids(_vid(url), name, kids=False):
            state, phase, pct, halt = "stalled", "Interrupted", None, True
            detail = f"nothing has changed for {int(quiet // 60)} min and the job is not running (PC restart or sleep?). Run it again from saved files"
    warns = []
    for l in log_lines[-60:]:
        if re.search(r"(?i)rror|CLAUDE LIMIT|failed|limit", l) and l not in warns:
            warns.append(l[:220])
    if "CLAUDE LIMIT" in live_log and state == "running":
        state = "warn"
    if state == "running" and any("rror" in w for w in warns[-2:]) and phase != "Done":
        state = "warn"

    steps = ["Download", "Transcribe", "Pick", "Render", "Done"]
    idx = {"Starting": 0, "Downloading": 0, "Download paused": 0, "Download stalled": 0, "Not started": 0,
           "Merging video and audio": 0, "Analysing sound and faces": 1, "Transcribing": 1, "Claude is reading the transcript": 2,
           "Picking moments": 2, "Rendering clips at 1080p": 3, "Done": 4, "Failed": -1}.get(phase, 0)
    # per-stage time plan: done stages show how long they took, the active one the time left, later ones a rough estimate
    dur = float(meta.get("duration", 0) or 0)
    wav = work / "_tmp" / "whole.wav"
    pst = past.stat().st_mtime if have_past else 0
    est_an = max(60.0, dur / 3600 * 55)
    est_tr = dur / (tr_rate or 30.0) if dur else 0
    n_clips = kept or made or max(5, int(dur / 2400))
    pm = _per_moment()
    n_mom = min(150, max(20, int(dur / 3600 * 8))) if dur else 40
    n_scout = int(dur / 600) + 1 if dur else 20
    est_pick = n_scout * _scout_secs() + n_mom * pm + 60.0
    est_rn = n_clips * 180.0
    cur = {"Starting": 0, "Downloading": 0, "Download paused": 0, "Download stalled": 0, "Not started": 0,
           "Merging video and audio": 0, "Analysing sound and faces": 1, "Transcribing": 2,
           "Claude is reading the transcript": 3, "Picking moments": 3, "Rendering clips at 1080p": 4, "Done": 5}.get(phase, 0)
    plan = []
    for i, nm_ in enumerate(["Download", "Analyse", "Transcribe", "Pick", "Render"]):
        e = dict(name=nm_, state="done" if i < cur else "active" if i == cur else "next", took=None, left=None, approx=False)
        if i == 0 and cur == 0:
            e["left"] = dl_eta
        elif i == 1:
            if i < cur and wav.exists() and pst:
                e["took"] = max(0.0, wav.stat().st_mtime - pst)
            elif i == cur:
                e["el"] = max(1.0, now - pst) if pst else 1.0
                e["left"], e["approx"] = max(60.0, est_an - e["el"]), True
            elif i > cur:
                e["left"], e["approx"] = est_an, True
        elif i == 2:
            if i == cur:
                if tr_left is not None:
                    e["left"] = tr_left
                    e["el"] = max(1.0, (pj["t"] - pj["t0"]) if pj else 1.0)
                else:
                    el = now - (wav.stat().st_mtime if wav.exists() else pst or now)
                    e["el"] = max(1.0, el)
                    e["left"], e["approx"] = max(120.0, est_tr - el), True
            elif i > cur:
                e["left"], e["approx"] = est_tr, True
        elif i == 3:
            if i == cur and not named and _scout_prog(live_log):
                k_, n_, per_ = _scout_prog(live_log)
                e["left"], e["approx"] = max(30.0, (n_ - k_) * (per_ or _scout_secs()) + n_mom * pm), True
            elif i == cur:
                tot_ = int(named.group(1)) if named else 0
                lr = _live_rate(live_log)
                e["left"], e["approx"] = (max(30.0, (tot_ - judged) / lr if lr else (tot_ - judged) * pm) if tot_ else est_pick), True
            elif i > cur:
                e["left"], e["approx"] = est_pick, True
        elif i == 4:
            if i == cur:
                e["left"], e["approx"] = (max(60.0, (made - len(clips)) * 180.0) if made else est_rn), True
            elif i > cur:
                e["left"], e["approx"] = est_rn, True
        plan.append(e)
    if state == "error" or halt:
        plan = []
    pct_est = pct
    if pct_est is None:
        act = next((e for e in plan if e["state"] == "active"), None)
        if act and act.get("el") and act.get("left") is not None:
            pct_est = min(0.95, act["el"] / (act["el"] + act["left"]))
    mtimes = [run.stat().st_mtime] + [f.stat().st_mtime for f in parts]
    ll = run / "live_log.txt"
    if ll.exists():
        mtimes.append(ll.stat().st_mtime)
    have_tr = bool(work.exists() and list(work.glob("transcript_*.json")))
    resumable = bool(have_past and (state in ("error", "stalled", "warn", "done") or paused_at))
    return dict(run=run.name, streamer=name, resumable=resumable, claude_wait=cw if state in ("running", "warn") else "",
                can_stop=state in ("running", "warn", "paused", "stalled") and phase != "Interrupted",
                can_delete=bool(have_past and state in ("done", "error", "stalled") and not (state == "error" and not have_tr)), saved=dict(video=bool(have_past), transcript=have_tr), title=meta.get("title", ""), url=url, mode=_mode(meta) if meta else "Catch-up",
                paused_at=paused_at, pause_req=pause_req if state == "running" else "", state=state, phase=phase, step=idx, steps=steps, plan=plan, pct_est=pct_est, pct_exact=pct is not None, pct=pct, detail=detail,
                duration=int(meta.get("duration", 0)), started=int(_started(run)), updated=int(max(mtimes)),
                clips=len(clips), moments=(int(picked.group(1)) if picked else (int(named.group(1)) if named else None)), named=(int(named.group(1)) if named else 0), judged=judged, kept=kept, made=(made or 0), warns=warns[-3:], tail=[l[:220] for l in log_lines[-6:]])


def build(paused=None) -> dict:
    now = time.time()
    out = []
    for run in sorted(BASE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if BASE.exists() else []:
        if run.is_dir() and re.search(r"_\d{8}_\d{6}$", run.name) and ((run / "catchup").exists() or (run / "live_log.txt").exists()):
            try:
                out.append(job(run, now, paused or set()))
            except Exception as ex:  # noqa: BLE001
                out.append(dict(run=run.name, streamer=_nm(run), state="error", phase="Could not read this run",
                                detail=str(ex)[:160], steps=[], step=0, pct=None, warns=[], tail=[], clips=0,
                                started=int(_started(run)), updated=int(now), mode="", title="", url="", duration=0))
    order = {"running": 0, "warn": 0, "stalled": 1, "paused": 1, "error": 2, "done": 3}
    out.sort(key=lambda j: (order.get(j["state"], 2), -j["updated"]))
    return dict(now=int(now), jobs=out)


# ---------------------------------------------------------------- start a run from a link
def _slug(n: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_")


def _heavy_busy(exclude=None) -> str:
    """Name and phase of a job that is analysing or transcribing right now (only one of those at a time is safe)."""
    now = time.time()
    for o in BASE.iterdir() if BASE.exists() else []:
        if o.is_dir() and o != exclude and re.search(r"_\d{8}_\d{6}$", o.name) and (o / "catchup").exists():
            try:
                x = job(o, now, set())
            except Exception:  # noqa: BLE001
                continue
            if x["state"] in ("running", "warn") and x["phase"] in ("Transcribing", "Analysing sound and faces"):
                return f"{x['streamer']} is {x['phase'].lower()}"
    return ""


def start(body: dict, creators_f: Path, yt_extra, event) -> dict:
    url = str(body.get("url", "")).strip()
    vid = _vid(url)
    if not vid:
        return {"error": "Paste a YouTube video or live link."}
    _YTX[0] = yt_extra
    busy = _heavy_busy()
    if busy:
        return {"error": f"Wait: {busy}. Two long jobs at once can run the PC out of memory. Add this one when it moves on to picking."}
    for run in BASE.iterdir() if BASE.exists() else []:
        if run.is_dir() and _vid(_url(run)) == vid:
            j = job(run, time.time(), set())
            if j["state"] in ("running", "warn", "stalled", "paused"):
                return {"error": f"That video is already being processed ({j['phase']})."}
            if not body.get("again"):
                return {"error": f"That video was already processed ({j['phase']}, {j['clips']} clips, run {run.name}). "
                                 + ("Use Run again from saved files on its card." if j.get("resumable") else "Tick Process again to redo it."),
                        "again_ok": True}
    free = shutil.disk_usage(HERE).free / 1e9
    if free < 25 and not body.get("force"):
        return {"error": f"Only {free:.0f} GB of disk is free. A long stream needs about 25 GB. Free some space (Jobs card, Delete source video) first."}
    try:
        data = json.loads(_read(creators_f))
    except ValueError:
        return {"error": "creators.json could not be read."}
    creators = data.get("creators", [])
    try:
        r = subprocess.run(["yt-dlp", *yt_extra(), "--skip-download", "--no-warnings", "--print",
                            "%(channel)s|%(channel_id)s|%(uploader_id)s|%(live_status)s|%(title)s", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        ch, cid, uid, ls, title = (r.stdout.strip().splitlines()[-1].split("|", 4) + [""] * 5)[:5]
    except Exception:  # noqa: BLE001
        ch = cid = uid = ls = title = ""
    if not (ch or title):
        return {"error": "Could not open that link. It may be private, removed, age-restricted or not a YouTube video."}
    want = str(body.get("name", "")).strip()
    hit = None
    if want:
        hit = next((c for c in creators if c["name"].lower() == want.lower()), None)
    if not hit and cid:
        hit = next((c for c in creators if c.get("channel_id") == cid), None)
    if not hit and ch:
        low = ch.lower()
        hit = next((c for c in creators if c["name"].lower() in low or low in c["name"].lower()), None)
    if not hit:
        lang = str(body.get("language", "")).strip().lower()
        if lang not in ("en", "hi"):
            lang = "hi" if any("\u0900" <= c <= "\u097f" for c in (title or "") + (ch or "")) else "en"
        want = want or (ch or "").strip()
        if not want:
            return {"error": "Could not tell whose channel this is."}
        hit = {"name": want, "channel_id": cid, "uploader_id": uid if str(uid).startswith("@") else "",
               "permission": "approved (owner, direct link " + time.strftime("%d %b %Y") + ")", "language": lang}
        creators.append(hit)
        creators_f.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        event(f"Added {want} to the list from a link.")
    if hit.get("language") not in ("en", "hi"):
        return {"error": f"{hit['name']} has no caption language in creators.json."}
    log = HERE / f"job_{_slug(hit['name'])}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    lf = open(log, "ab")
    mode = "game" if str(body.get("mode", "")).lower() == "game" else "face"
    subprocess.Popen([sys.executable, "-u", str(HERE / "run_streamer.py"), hit["name"], "--url",
                      f"https://www.youtube.com/watch?v={vid}", "--mode", mode], cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=lf,
                     stderr=subprocess.STDOUT, creationflags=(0x08000000 | 0x00000200) if sys.platform == "win32" else 0)
    event(f"Started {hit['name']} from a link ({'live' if ls == 'is_live' else 'catch-up'}, {'gameplay only' if mode == 'game' else 'facecam + gameplay'}): {title[:60]}")
    return {"ok": True, "streamer": hit["name"], "mode": "live" if ls == "is_live" else "catch-up", "title": title,
            "note": "This stream is still live, so only what is recorded so far is clipped." if ls == "is_live" else ""}


def resume(body: dict) -> dict:
    """Run a finished or stopped job again from what is already saved (video, transcript, sound and face analysis)."""
    rn = str(body.get("run", "")).strip()
    run = BASE / rn
    if not rn or "/" in rn or "\\" in rn or ".." in rn or not run.is_dir() or not (run / "catchup" / "past.mp4").exists():
        return {"error": "Nothing saved to run again from."}
    now = time.time()
    me = job(run, now, set())
    if me["state"] == "running" or (me["state"] == "paused" and not me.get("paused_at")):
        return {"error": f"This job is still running ({me['phase']})."}
    if me["state"] == "stalled" and _pids(_vid(_url(run)), _nm(run), kids=False):
        return {"error": "This job is still running."}
    if not me["saved"]["transcript"]:
        busy = _heavy_busy(run)
        if busy:
            return {"error": f"Wait: {busy}. Two long transcriptions at once can run the PC out of memory."}
    for nm_ in ("stopped.txt", "paused.txt", "pause_req.txt"):
        try:
            (run / nm_).unlink()
        except OSError:
            pass
    url = _url(run)
    if not url:
        return {"error": "The link for this job is missing."}
    log = HERE / f"job_{_slug(_nm(run))}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    lf = open(log, "ab")
    subprocess.Popen([sys.executable, "-u", str(HERE / "run_streamer.py"), _nm(run), "--url", url, "--resume", rn], cwd=str(HERE),
                     stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                     creationflags=(0x08000000 | 0x00000200) if sys.platform == "win32" else 0)
    return {"ok": True, "streamer": _nm(run), "from": "transcript" if me["saved"]["transcript"] else "video"}


def stop(body: dict) -> dict:
    """Stop a running job cleanly: ends run_streamer and its clipper, yt-dlp and ffmpeg children. Saved files stay."""
    rn = str(body.get("run", "")).strip()
    run = BASE / rn
    if not rn or "/" in rn or "\\" in rn or ".." in rn or not run.is_dir():
        return {"error": "Unknown job."}
    pids = _pids(_vid(_url(run)), _nm(run))
    for p in pids:
        subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    (run / "stopped.txt").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    with open(run / "live_log.txt", "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + "      stopped by you\n")
    return {"ok": True, "stopped": len(pids)}


_TRANSIENT = ("Merging video and audio", "Starting", "Not started")


def request_pause(body: dict) -> dict:
    """Ask a running job to pause at the END of its current stage (when that stage's results are saved).
    on=False cancels the request. The job is only stopped by the watcher below, never mid-stage."""
    rn = str(body.get("run", "")).strip()
    run = BASE / rn
    if not rn or "/" in rn or "\\" in rn or ".." in rn or not run.is_dir():
        return {"error": "Unknown job."}
    rq = run / "pause_req.txt"
    if body.get("on") is False:
        try:
            rq.unlink()
        except OSError:
            pass
        return {"ok": True, "pause": False}
    j = job(run, time.time(), set())
    if j["state"] != "running":
        return {"error": f"Nothing to pause ({j['phase']})."}
    rq.write_text(j["phase"], encoding="utf-8")
    return {"ok": True, "pause": True, "after": j["phase"]}


def _watch() -> None:
    while True:
        time.sleep(4)
        try:
            for rq in BASE.glob("*/pause_req.txt"):
                run, req = rq.parent, _read(rq).strip()
                j = job(run, time.time(), set())
                if j["state"] in ("done", "error", "stalled"):
                    rq.unlink(missing_ok=True)
                elif j["state"] == "running" and j["phase"] != req and j["phase"] not in _TRANSIENT:
                    (run / "paused.txt").write_text(req, encoding="utf-8")  # first, so the job never shows "Stopped by you"
                    for p in _pids(_vid(_url(run)), _nm(run)):
                        subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"], capture_output=True, creationflags=0x08000000)
                    with open(run / "live_log.txt", "a", encoding="utf-8") as f:
                        f.write(time.strftime("%H:%M:%S ") + f"      paused after {req}\n")
                    rq.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_watch, daemon=True).start()


def delete_run(body: dict) -> dict:
    """Delete a run folder forever (video, audio, transcript, clips). Stops its processes first. Needs confirm=true."""
    rn = str(body.get("run", "")).strip()
    run = BASE / rn
    if not rn or "/" in rn or "\\" in rn or ".." in rn or not run.is_dir() or not re.search(r"_\d{8}_\d{6}$", rn):
        return {"error": "Unknown job."}
    if body.get("confirm") is not True:
        return {"error": "Not confirmed."}
    for p in _pids(_vid(_url(run)), _nm(run)):
        subprocess.run(["taskkill", "/PID", str(p), "/T", "/F"], capture_output=True, creationflags=0x08000000)
    time.sleep(1.5)
    size = sum(f.stat().st_size for f in run.rglob("*") if f.is_file())
    shutil.rmtree(run, ignore_errors=True)
    if run.exists():
        return {"error": "Some files are still in use. Try again in a few seconds."}
    return {"ok": True, "freed_gb": round(size / 1e9, 2)}


def delete_source(body: dict) -> dict:
    """Free disk: remove the big downloaded video and the temp audio of a finished or stopped job (transcripts stay)."""
    rn = str(body.get("run", "")).strip()
    run = BASE / rn
    if not rn or "/" in rn or "\\" in rn or ".." in rn or not run.is_dir():
        return {"error": "Unknown job."}
    me = job(run, time.time(), set())
    if me["state"] in ("running", "warn", "paused"):
        return {"error": "Stop the job first."}
    freed = 0
    for f in (run / "catchup" / "past.mp4", run / "catchup" / "work" / "_tmp" / "whole.wav"):
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return {"ok": True, "freed_gb": round(freed / 1e9, 1)}
