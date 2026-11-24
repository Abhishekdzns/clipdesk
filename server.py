"""Clipdesk server: local dashboard + streamer watcher. Standard library only. Run with run_clipdesk.bat.
Serves ui/index.html, reads clips and logs from runs/live, auto-starts live mode when an allowlisted streamer goes live."""
import csv
import glob
import hashlib
import json
import random
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import jobs_api
except Exception:  # noqa: BLE001 - Clipdesk still runs without the Jobs page
    jobs_api = None

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs" / "live"
STATE_F = HERE / "clipdesk_state.json"
CREATORS_F = HERE / "creators.json"
USAGE_F = HERE / "claude_usage.jsonl"
PORT = 8765
MAX_STREAMS = 3
POLL_S = 5
LOCK = threading.RLock()
EVENTS: list = []          # newest first: [hh:mm, text]
PROCS: dict = {}           # streamer name -> Popen
LIVE: dict = {}            # streamer name -> {"live": bool, "id": str, "title": str, "t0": float}
BACKOFF: dict = {}         # streamer name -> earliest restart time
CPU = {"pct": None}
RES = {"ram_pct": None, "ram_used": None, "ram_total": None, "gpu_pct": None, "vram_used": None, "vram_total": None, "gpu_temp": None}
CACHE: dict = {}

DEFAULT_STATE = {
    "paused": False,
    "mode": "review",
    "streamers": {},       # name -> {"auto": bool}
    "clips": {},           # id -> {"status": "new|approved|rejected", "title": str}
    "platforms": [
        {"id": "yt", "name": "YouTube Shorts", "on": False, "conn": False, "cap": 6,
         "note": "Free API. New projects allow about 6 uploads a day until Google approves a quota raise."},
        {"id": "ig", "name": "Instagram Reels", "on": False, "conn": False, "cap": 10,
         "note": "Needs a business account linked to a Facebook page and a Meta app. Free."},
        {"id": "x", "name": "X", "on": False, "conn": False, "cap": 10,
         "note": "Posting video needs a paid API tier. Free tier is very limited."},
        {"id": "tt", "name": "TikTok", "on": False, "conn": False, "cap": 5,
         "note": "Posting needs app approval from TikTok. Not a launch week job."},
    ],
    "window": {"from": "10:00", "to": "23:00", "gap": 45},
}


def now_hm() -> str:
    return time.strftime("%H:%M")


def event(msg: str) -> None:
    with LOCK:
        EVENTS.insert(0, [now_hm(), msg])
        del EVENTS[60:]


def load_state() -> dict:
    with LOCK:
        try:
            d = json.loads(STATE_F.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        out = json.loads(json.dumps(DEFAULT_STATE))
        out.update({k: v for k, v in d.items() if k in out})
        return out


def save_state(st: dict) -> None:
    with LOCK:
        tmp = STATE_F.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(STATE_F)


def load_creators() -> list:
    try:
        return json.loads(CREATORS_F.read_text(encoding="utf-8")).get("creators", [])
    except Exception:  # noqa: BLE001
        return []


def perm_of(text: str) -> str:
    t = (text or "").lower()
    if "not asked" in t or "test" in t:
        return "test"
    if "asked" in t or "waiting" in t:
        return "asked"
    if "approved" in t or "yes" in t or "granted" in t:
        return "approved"
    return "test"


def yt_extra() -> list:
    sys.path.insert(0, str(HERE))
    import clipper  # noqa: PLC0415
    return clipper.yt_extra()


# ------------------------------------------------------------ watcher
def check_live(ch_id: str):
    cmd = ["yt-dlp", *yt_extra(), "--skip-download", "--no-warnings", "--playlist-items", "1",
           "--print", "%(is_live)s|%(id)s|%(title)s", f"https://www.youtube.com/channel/{ch_id}/live"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    except Exception:  # noqa: BLE001
        return None
    for line in r.stdout.splitlines():
        p = line.split("|", 2)
        if len(p) == 3 and p[0] == "True":
            return {"id": p[1], "title": p[2]}
    return {}


def run_folder_fresh(name: str, secs: float = 75):
    """A live run for this streamer already writing its log (for example started by hand)."""
    key = name.replace(" ", "_")
    best = None
    if RUNS.exists():
        for d in RUNS.glob(f"{key}_*"):
            lf = d / "live_log.txt"
            if lf.exists() and time.time() - lf.stat().st_mtime < secs:
                if best is None or lf.stat().st_mtime > best.stat().st_mtime:
                    best = lf
    return best.parent if best else None


def start_live(c: dict, info: dict) -> None:
    name = c["name"]
    handle = (c.get("uploader_id") or name.replace(" ", "")).lstrip("@")
    logd = HERE / "runs" / "watcher"
    logd.mkdir(parents=True, exist_ok=True)
    lf = open(logd / f"{name.replace(' ', '_')}.log", "ab")
    url = f"https://www.youtube.com/watch?v={info['id']}"
    cmd = [sys.executable, "-u", str(HERE / "live.py"), url, "--name", name, "--handle", handle,
           "--video-title", info.get("title", ""), "--brain-topic", "live stream, GTA VI reactions",
           "--catchup-min", "15", "--catchup-top", "2"]
    if c.get("language") == "hi":
        cmd.append("--hinglish")
    if c.get("facecam"):
        cmd += ["--facecam", c["facecam"]]
    lang = c.get("language")
    if lang not in ("hi", "en"):  # CLIP RULES: never guess the caption language
        event(f"{name} went live but has no language set (en or hi) in creators.json. Not clipping: ask the owner.")
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    PROCS[name] = subprocess.Popen(cmd, cwd=str(HERE), stdout=lf, stderr=subprocess.STDOUT, creationflags=flags,
                                   env=dict(os.environ, CLIPPER_LANG=lang))
    event(f"{name} went live. Watching started.")


SWEEP_AT, SWEEP_P = 0.0, None


def watcher() -> None:
    time.sleep(3)
    while True:
        try:
            st = load_state()
            creators = load_creators()
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
            todo = [c for c in creators if c.get("channel_id") and not st["paused"]
                    and st["streamers"].get(c["name"], {}).get("auto", True)]
            with ThreadPoolExecutor(max_workers=6) as ex:
                INFOS = dict(zip([c["name"] for c in todo], ex.map(lambda c: check_live(c["channel_id"]), todo)))
            for c in creators:
                name = c["name"]
                cfg = st["streamers"].get(name, {})
                p = PROCS.get(name)
                if p and p.poll() is not None:
                    PROCS.pop(name, None)
                    event(f"{name}: watching stopped (stream ended or app closed it).")
                    BACKOFF[name] = time.time() + 180
                if st["paused"]:
                    continue
                if not cfg.get("auto", True) or not c.get("channel_id"):
                    continue
                running = name in PROCS or run_folder_fresh(name)
                info = INFOS.get(name)
                with LOCK:
                    if info is None:
                        pass
                    elif info:
                        LIVE[name] = {"live": True, "id": info["id"], "title": info["title"],
                                      "t0": LIVE.get(name, {}).get("t0") or time.time()}
                    else:
                        LIVE[name] = {"live": False, "id": "", "title": "", "t0": 0}
                if info and not running and time.time() >= BACKOFF.get(name, 0) and len(PROCS) < MAX_STREAMS:
                    start_live(c, info)
            global SWEEP_AT, SWEEP_P
            if (HERE / "sweep_todo.json").exists() and time.time() >= SWEEP_AT and (SWEEP_P is None or SWEEP_P.poll() is not None):
                SWEEP_AT = time.time() + 1800
                SWEEP_P = subprocess.Popen([sys.executable, "-u", str(HERE / "sweep.py")], cwd=str(HERE),
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as ex:  # noqa: BLE001
            event(f"Watcher error: {str(ex)[:120]}")
        time.sleep(POLL_S)

POST_P = {"p": None, "cid": None, "slot": 0}


def publisher() -> None:
    """Checks for approved clips every 5 s, so uploads start right after approval."""
    time.sleep(2)
    try:  # a restart kills running uploads: put orphaned "posting" clips back in line
        with LOCK:
            st = load_state()
            for c in st.get("clips", {}).values():
                if c.get("status") == "posting":
                    c["status"] = "approved"
                if c.get("ig") == "posting":
                    c.pop("ig", None)
            save_state(st)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    while True:
        try:
            post_tick()
        except Exception as ex:  # noqa: BLE001
            event(f"Posting error: {str(ex)[:120]}")
        try:
            ig_tick()
        except Exception as ex:  # noqa: BLE001
            event(f"Instagram error: {str(ex)[:120]}")
        time.sleep(5)



def next_slot(kind: str, st: dict) -> int:
    """Schedule mode: when the next clip should go live. At least 25 min from now, and one gap after the previous scheduled one."""
    gap = int(st.get("window", {}).get("gap", 10)) * 60
    return int(max(time.time() + 35 * 60, st.get("sched_last", {}).get("both", 0) + gap))  # one slot per clip, same time on YouTube and Instagram


def claim_slot(cid: str) -> int:
    """One go-live time per clip, shared by YouTube and Instagram. Whichever starts first claims it."""
    with LOCK:
        st = load_state()
        c = st["clips"].setdefault(cid, {})
        if c.get("sched_at") and int(c["sched_at"]) > time.time() + 22 * 60:
            return int(c["sched_at"])
        gap = int(st.get("window", {}).get("gap", 10)) * 60
        last = int(st.get("sched_last", {}).get("both", 0))
        for k, v in st["clips"].items():
            if k != cid:
                last = max(last, int(v.get("sched_at") or 0), int(v.get("yt_sched") or 0), int(v.get("ig_sched") or 0))
        slot = int(max(time.time() + 35 * 60, last + gap))
        c["sched_at"] = slot
        st.setdefault("sched_last", {})["both"] = slot
        save_state(st)
        return slot


def post_tick() -> None:
    """Auto posting (CLIP RULES 0): approved clips only, streamer permission approved only, inside the posting window,
    a gap between posts, a daily cap, one at a time. YouTube Shorts for now."""
    p = POST_P["p"]
    if p is not None:
        if p.poll() is None:
            return
        out = (p.stdout.read() if p.stdout else "") or ""
        cid = POST_P["cid"]
        POST_P["p"] = None
        m = re.search(r'\{"id": "([^"]+)", "privacy": "([^"]*)"\}', out)
        with LOCK:
            st = load_state()
            c = st["clips"].setdefault(cid, {})
            if m:
                slot_ = POST_P.get("slot") or 0
                c.update(status="posted", yt=m.group(1), yt_privacy=("scheduled" if slot_ else m.group(2)), posted_at=int(time.time()))
                if slot_:
                    c["yt_sched"] = slot_
                    c["sched_at"] = slot_
                    st.setdefault("sched_last", {})["both"] = max(slot_, st.get("sched_last", {}).get("both", 0))
                st.setdefault("post_log", []).append([int(time.time()), cid, m.group(1)])
            else:
                c.update(status="approved", post_error=out[-200:])
            save_state(st)
        event(f"YouTube: {'posted' if m else 'upload FAILED for'} {c.get('title', cid)[:40]}" + (f" ({m.group(2)}) https://youtu.be/{m.group(1)}" if m else ""))
        return
    st = load_state()
    linked = (HERE / "yt_token.json").exists()
    yt = next((x for x in st.get("platforms", []) if x["id"] == "yt"), {})
    if yt and yt.get("conn") != linked:
        with LOCK:
            st2 = load_state()
            for x in st2.get("platforms", []):
                if x["id"] == "yt":
                    x["conn"] = linked
            save_state(st2)
    if not yt.get("on") or not linked:  # Pause/Resume only controls stream watching, never posting (owner, 21 Nov)
        return
    w = st.get("window", {})
    if not w.get("sched") and not (w.get("from", "00:00") <= now_hm() <= w.get("to", "23:59")):
        return
    log = st.get("post_log", [])
    day = time.strftime("%Y-%m-%d")
    today = [x for x in log if time.strftime("%Y-%m-%d", time.localtime(x[0])) == day]
    sched = bool(w.get("sched"))
    if len(today) >= int(yt.get("cap", 6)):
        return
    if not sched and log and time.time() - log[-1][0] < int(w.get("gap", 45)) * 60:
        return
    perms = {c["name"]: perm_of(c.get("permission", "")) for c in load_creators()}
    for c in sorted(gather_clips(st), key=lambda x: x["_t"]):
        if c["status"] != "approved":
            continue
        if perms.get(c["who"]) != "approved":
            continue  # CLIP RULES: never post a streamer who has not given permission
        POST_P["cid"] = c["id"]
        POST_P["slot"] = claim_slot(c["id"]) if sched else 0
        POST_P["p"] = subprocess.Popen([sys.executable, "-u", str(HERE / "yt_poster.py"), "post", c["_p"], c["who"]] + ([str(POST_P["slot"])] if sched else []), cwd=str(HERE),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with LOCK:
            st2 = load_state()
            st2["clips"].setdefault(c["id"], {})["status"] = "posting"
            save_state(st2)
        event(f"YouTube: {'scheduling' if sched else 'uploading'} {c['title'][:40]}")
        return



IG_P = {"p": None, "cid": None, "slot": 0}
IG_MBS = {"p": None}
IG_LOGIN = {"p": None}
IG_JIT = [0]  # extra 1 to 8 min on top of the gap, re-rolled after every post, so posts are not on a fixed beat


def ig_tick() -> None:
    """Instagram Reels through the signed-in browser profile (ig_poster.py). Posts clips that are approved or already
    posted on YouTube and not yet on Instagram. One at a time, same gap and cap rules. A failed clip is not retried by itself."""
    lp = IG_LOGIN["p"]
    if lp is not None and lp.poll() is not None:
        IG_LOGIN["p"] = None
        if (HERE / "ig_account.txt").exists():
            with LOCK:
                st = load_state()
                for c in st.get("clips", {}).values():
                    if c.get("ig") in ("error", "posting"):
                        c.pop("ig", None); c.pop("ig_error", None)
                        if c.get("status") == "posting_ig":
                            c["status"] = "approved"
                save_state(st)
            event("Instagram: signed in as " + (HERE / "ig_account.txt").read_text(encoding="utf-8").strip() + ". Failed clips can post again.")
    p = IG_P["p"]
    if p is not None:
        if p.poll() is None:
            return
        out = (p.stdout.read() if p.stdout else "") or ""
        cid = IG_P["cid"]
        IG_P["p"] = None
        ok = '"posted"' in out or '"scheduled"' in out
        slot_ = IG_P.get("slot") or 0
        with LOCK:
            st = load_state()
            c = st["clips"].setdefault(cid, {})
            if ok:
                c.update(ig=("scheduled" if slot_ else "posted"), ig_at=int(time.time()))
                if slot_:
                    c["ig_sched"] = slot_
                    st.setdefault("sched_last", {})["both"] = max(slot_, st.get("sched_last", {}).get("both", 0))
                st.setdefault("ig_log", []).append([int(time.time()), cid])
                IG_JIT[0] = 0
                if c.get("status") == "posting_ig":
                    c["status"] = "posted"
            else:
                c.update(ig="error", ig_error=("not signed in" if "NOT LOGGED IN" in out else out[-200:]))
                if c.get("status") == "posting_ig":
                    c["status"] = "approved"
            save_state(st)
        event(f"Instagram: {'posted' if ok else 'upload FAILED for'} {c.get('title', cid)[:40]}")
        return
    st = load_state()
    acc = (HERE / "ig_account.txt").read_text(encoding="utf-8").strip() if (HERE / "ig_account.txt").exists() else ""
    linked = bool(acc) and (HERE / "ig_poster.py").exists()
    note = (f"Posts through the signed-in browser as {acc if acc == 'signed in' else '@' + acc}. Free." if linked
            else "Not signed in. Press Sign in to Instagram.")
    ig = next((x for x in st.get("platforms", []) if x["id"] == "ig"), {})
    if ig and (ig.get("conn") != linked or ig.get("note") != note):
        with LOCK:
            st2 = load_state()
            for x in st2.get("platforms", []):
                if x["id"] == "ig":
                    x["conn"] = linked
                    x["note"] = note
            save_state(st2)
    if not ig.get("on") or not linked:
        return
    w = st.get("window", {})
    sched = bool(w.get("sched"))
    if sched and not (HERE / "ig_mbs.txt").exists():
        return  # schedule mode needs Meta Business Suite connected (Publishing)
    if not sched and not (w.get("from", "00:00") <= now_hm() <= w.get("to", "23:59")):
        return
    log = st.get("ig_log", [])
    day = time.strftime("%Y-%m-%d")
    cap_ = int(ig.get("cap", 10))
    if cap_ > 0 and len([x for x in log if time.strftime("%Y-%m-%d", time.localtime(x[0])) == day]) >= cap_:  # cap 0 = no daily cap
        return
    if not sched and log and time.time() - log[-1][0] < int(w.get("gap", 45)) * 60 + IG_JIT[0]:
        return
    perms = {c["name"]: perm_of(c.get("permission", "")) for c in load_creators()}
    for c in sorted(gather_clips(st), key=lambda x: x["_t"]):
        if c["status"] not in ("approved", "posted") and not (sched and c["status"] == "posting"):
            continue
        cs = st["clips"].get(c["id"], {})
        if cs.get("ig") or perms.get(c["who"]) != "approved":
            continue  # already on Instagram, failed before (no auto retry), or no permission
        env = dict(os.environ, IG_HEADLESS="1")
        IG_P["cid"] = c["id"]
        IG_P["slot"] = 0
        if sched:
            want = claim_slot(c["id"])
            IG_P["slot"] = want if want - time.time() >= 22 * 60 else int(time.time() + 24 * 60)  # Meta needs 20+ min lead
        IG_P["p"] = subprocess.Popen([sys.executable, "-u", str(HERE / "ig_poster.py"), "post", c["_p"]] + (["--at", str(IG_P["slot"])] if sched else []), cwd=str(HERE), env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with LOCK:
            st2 = load_state()
            st2["clips"].setdefault(c["id"], {})["ig"] = "posting"
            save_state(st2)
        event(f"Instagram: uploading {c['title'][:40]}")
        return


# ------------------------------------------------------------ health
def keep_awake() -> None:
    if os.name != "nt":
        return
    import ctypes
    ES = 0x80000000 | 0x00000001  # continuous + system required
    while True:
        ctypes.windll.kernel32.SetThreadExecutionState(ES)
        time.sleep(30)


def cpu_loop() -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class FT(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def val(f):
        return (f.hi << 32) | f.lo

    def sample():
        i, k, u = FT(), FT(), FT()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
        return val(i), val(k) + val(u)

    a = sample()
    while True:
        time.sleep(4)
        b = sample()
        di, dt = b[0] - a[0], b[1] - a[1]
        CPU["pct"] = round(100 * (1 - di / dt)) if dt else None
        a = b


def res_loop() -> None:
    """Memory (RAM) and graphics card use, refreshed every 4 seconds, so the Jobs page can show if the PC is coping."""
    import subprocess
    if os.name == "nt":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tot", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                        ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                        ("ae", ctypes.c_ulonglong)]
    while True:
        try:
            if os.name == "nt":
                m = MS()
                m.len = ctypes.sizeof(MS)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                RES["ram_total"] = round(m.tot / 2**30, 1)
                RES["ram_used"] = round((m.tot - m.avail) / 2**30, 1)
                RES["ram_pct"] = int(m.load)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=6, creationflags=0x08000000 if os.name == "nt" else 0)
            p = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
            RES["gpu_pct"], RES["vram_used"], RES["vram_total"] = int(p[0]), round(int(p[1]) / 1024, 1), round(int(p[2]) / 1024, 1)
            RES["gpu_temp"] = int(p[3])
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)


def dir_size(p: Path) -> int:
    n = 0
    for root, _d, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


LOGC = {}
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
THUMBS = HERE / "cache" / "thumbs"


def thumb_for(f: Path) -> Path | None:
    """A small JPEG still of a clip, made once with ffmpeg and kept, so the dashboard shows pictures, not videos."""
    st_ = f.stat()
    p = THUMBS / f"{clip_id(f)}_{int(st_.st_mtime)}.jpg"
    if p.exists():
        return p
    THUMBS.mkdir(parents=True, exist_ok=True)
    for old in THUMBS.glob(f"{clip_id(f)}_*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "3", "-i", str(f), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "5", str(p)], timeout=30, **NOWIN)
    except Exception:  # noqa: BLE001
        return None
    return p if p.exists() else None


def cached(key, ttl, fn):
    t, v = CACHE.get(key, (0, None))
    if time.time() - t > ttl:
        v = fn()
        CACHE[key] = (time.time(), v)
    return v


def claude_usage() -> dict:
    calls, usd, cut = 0, 0.0, time.time() - 5 * 3600
    try:
        for line in USAGE_F.read_text(encoding="utf-8").splitlines()[-4000:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("t", 0) >= cut:
                calls += 1
                usd += r.get("usd", 0)
    except OSError:
        pass
    return {"calls": calls, "usd": round(usd, 2)}


# ------------------------------------------------------------ clips and logs
def ffprobe_len(p: Path) -> int:
    def run():
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                               capture_output=True, text=True, timeout=20)
            return int(float(r.stdout.strip()))
        except Exception:  # noqa: BLE001
            return 0
    return cached(("len", str(p), p.stat().st_mtime), 10 ** 9, run)


def reports(run_dir: Path) -> dict:
    """Only look where report.csv files live (rglob also walked the stream buffer every few seconds)."""
    out = {}
    rps = [*run_dir.glob("report.csv"), *run_dir.glob("*/report.csv"), *run_dir.glob("*/*/report.csv")]
    for rp in rps:
        is_cu = "catchup" in rp.parts
        try:
            with open(rp, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    fn = row.get("file") or ""
                    if fn:
                        out[(is_cu, fn)] = row
        except Exception:  # noqa: BLE001
            pass
    return out


def clip_id(p: Path) -> str:
    return hashlib.md5(str(p.relative_to(HERE)).encode()).hexdigest()[:10]


def who_of(d: Path) -> str:
    return re.sub(r"_\d{8}_\d{6}$", "", d.name).replace("_", " ")


def clip_files(d: Path) -> list:
    """Clips in a run folder: the clips folder, plus any catch-up test folder whose clips have a caption sidecar."""
    fs = sorted((d / "clips").glob("*.mp4")) if (d / "clips").exists() else []
    cu = d / "catchup"
    if cu.exists():
        fs += [f for f in sorted(cu.glob("*/clip_*.mp4")) if f.with_suffix(".edit.json").exists()]
    return fs


STG = {}


def _stage_logs(d: Path) -> dict:
    """Which clip stem each log is working on right now (cached by file mtime)."""
    out = {"remaster": "", "reframe": "", "reframe_on": False}
    rl = d.parent.parent.parent / "reframe_log.txt"
    try:
        out["reframe_on"] = (rl.exists() and "REFRAME DONE" not in rl.read_text(encoding="utf-8", errors="replace")) or (not rl.exists() and (rl.parent / "reframe_test_log.txt").exists())
    except OSError:
        pass
    for key, pat, rx in (("remaster", "remaster*log*.txt", r"^\[(\d+)/(\d+)\] [^/\\]+[/\\](.+?)\.edit"), ("reframe", "reframe*log*.txt", r"^== (.+)$")):
        best = None
        for lf in d.parent.parent.parent.glob(pat):
            try:
                if best is None or lf.stat().st_mtime > best.stat().st_mtime:
                    best = lf
            except OSError:
                pass
        if best is None or time.time() - best.stat().st_mtime > 600:
            continue
        try:
            ln = best.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        cur, fin = "", False
        for x in ln:
            x = re.sub(r"^\d\d:\d\d:\d\d\s?", "", x)
            m = re.match(rx, x)
            if m:
                cur, fin = m.group(m.lastindex).strip(), False
            elif re.match(r"^\s+(ok |.*ok framed|.*FAILED|.*STOP|.*NOT 1080p)", x) or "done already" in x:
                fin = True
        out[key] = "" if fin else cur
    return out


def clip_stage(d: Path, f: Path, sd: dict, logs: dict) -> dict:
    stem = f.stem

    def live(label, lo, hi, secs):
        k = (stem, label)
        t0 = STG.setdefault(k, time.time())
        return {"stage": label, "pct": int(lo + min(1.0, (time.time() - t0) / secs) * (hi - lo)), "busy": True}
    if logs.get("reframe") and logs["reframe"] == stem:
        r = live("Framing face + chest", 90, 99, 60)
        r["frm"] = 0
        return r
    if logs.get("remaster") and logs["remaster"] == stem:
        return live("Rendering 1080p", 40, 90, 200)
    if sd.get("reframe") == 1:
        return {"stage": "Final 1080p, framed", "pct": 100, "busy": False, "frm": 1}
    if sd.get("hq"):
        if logs.get("reframe_on"):
            return {"stage": "Waiting for framing fix", "pct": 90, "busy": False, "frm": 0}
        return {"stage": "Final 1080p", "pct": 100, "busy": False}
    if sd:
        return {"stage": "480p draft, waiting for 1080p", "pct": 35, "busy": False}
    return {"stage": "", "pct": 100, "busy": False}


def _live_both(cs: dict, x: dict) -> bool:
    now = time.time()
    yt_ok = x["status"] == "posted" and not (cs.get("yt_sched", 0) > now)
    ig_ok = cs.get("ig") == "posted" or (cs.get("ig") == "scheduled" and cs.get("ig_sched", 0) <= now)
    return yt_ok and ig_ok


def cleanable(st: dict, clips: list, name: str) -> bool:
    """True when every kept clip of this streamer is live (posted) on YouTube and Instagram."""
    mine = [x for x in clips if x["who"] == name and x["status"] != "rejected" and not st["clips"].get(x["id"], {}).get("bin")]
    return bool(mine) and all(_live_both(st["clips"].get(x["id"], {}), x) for x in mine)


def gather_clips(st: dict) -> list:
    out = []
    if not RUNS.exists():
        return out
    for d in sorted(RUNS.iterdir()):
        files = clip_files(d)
        if not files:
            continue
        reps = reports(d)
        logs = _stage_logs(d)
        for i, f in enumerate(files):
            cid = clip_id(f)
            is_cu = f.name.startswith("catchup") or f.parent.parent.name == "catchup"
            m = re.search(r"(clip_.*\.mp4)$", f.name)
            row = reps.get((is_cu, m.group(1) if m else f.name)) or reps.get((is_cu, f.name)) or {}
            title = (row.get("tweet") or "").split(": ", 1)[-1] or f.stem
            try:
                score = int(float(row.get("score") or 0))
            except ValueError:
                score = 0
            c = st["clips"].get(cid, {})
            info = ""
            sd = {}
            try:
                sd = json.loads(f.with_suffix(".edit.json").read_text(encoding="utf-8")) if f.with_suffix(".edit.json").exists() else {}
                info = str(sd.get("info", "")).split(" | ")[0]
                title = sd.get("title") or title
            except Exception:  # noqa: BLE001
                pass
            out.append({"id": cid, "who": who_of(d), "info": info, "title": c.get("title") or title, "score": score,
                        "len": ffprobe_len(f), "src": "Catch-up" if is_cu else "Live",
                        "when": time.strftime("%H:%M", time.localtime(f.stat().st_mtime)),
                        "stage": (stg := clip_stage(d, f, sd, logs))["stage"], "pct": stg["pct"], "busy": stg["busy"], "frm": stg.get("frm", -1), "status": c.get("status", "new"), "bin": bool(c.get("bin")), "yt": c.get("yt", ""), "ig": c.get("ig", ""), "edit": f.with_suffix(".edit.json").exists(), "c": len(out), "v": int(f.stat().st_mtime), "_t": f.stat().st_mtime, "_p": str(f)})
    out.sort(key=lambda x: -x["_t"])
    return out


def run_info() -> dict:
    """Newest run folder per streamer: log freshness, start time, chat series."""
    info = {}
    if not RUNS.exists():
        return info
    for d in sorted(RUNS.iterdir()):
        lf = d / "live_log.txt"
        if not lf.exists():
            continue
        who = who_of(d)
        try:
            stt = lf.stat()
            key = ("log", str(lf), stt.st_mtime, stt.st_size)
            if key not in LOGC:  # only re-read a log when it changed
                LOGC.clear() if len(LOGC) > 200 else None
                LOGC[key] = lf.read_text(encoding="utf-8", errors="replace").splitlines()
            lines = LOGC[key]
        except OSError:
            continue
        chat = [float(m.group(1)) for ln in lines[-40:] if (m := re.search(r"chat=\s*([\d.]+)", ln))][-15:]
        pt = None
        for ln in reversed(lines):
            m = re.search(r" piece\s+\d+ t=(\d+):(\d\d)", ln)
            if m:
                pt = int(m.group(1)) * 60 + int(m.group(2))
                break
        lm = None
        for ln in reversed(lines):
            m = re.search(r"MOMENT at (\d+):(\d\d)", ln)
            if m:
                lm = int(m.group(1)) * 60 + int(m.group(2))
                break
        cu = [ln for ln in lines if "catch-up" in ln.lower()]
        cu_last = cu[-1].lower() if cu else ""
        cu_busy = bool(cu) and not any(k in cu_last for k in ("done", "skipped", "failed", "nothing to catch", "limit hit", "could not"))
        txt = "\n".join(lines)
        prog = {"piece_t": pt, "moments": txt.count("MOMENT at"), "made": txt.count("CLIP READY"),
                "no": txt.count("judge said no clip"), "catchup": cu_busy, "last_m": lm,
                "ended": "stream ended" in txt or "Done." in txt}
        info[who] = {"prog": prog, "fresh": time.time() - lf.stat().st_mtime < 75, "t0": d.stat().st_ctime,
                     "series": chat, "lines": [ln for ln in lines if " piece " not in ln][-8:]}
    return info


def build_state() -> dict:
    st = load_state()
    creators = load_creators()
    runs = run_info()
    clips = gather_clips(st)
    streamers = []
    for i, c in enumerate(creators):
        name = c["name"]
        r = runs.get(name, {})
        lv = LIVE.get(name, {})
        live = bool(r.get("fresh")) or bool(lv.get("live"))
        t0 = r.get("t0") if r.get("fresh") else lv.get("t0")
        since = ""
        if live and t0:
            m = int((time.time() - t0) // 60)
            since = f"{m // 60}h {m % 60:02d}m"
        streamers.append({"id": name, "name": name, "cid": c.get("channel_id", ""), "handle": c.get("uploader_id") or c.get("channel_id", ""),
                          "live": live, "since": since, "perm": perm_of(c.get("permission", "")),
                          "clips": sum(1 for x in clips if x["who"] == name),
                          "cleanable": cleanable(st, clips, name),
                          "auto": st["streamers"].get(name, {}).get("auto", True),
                          "prog": (r.get("prog") if r.get("fresh") else None) or ({"starting": True} if name in PROCS else None),
                          "series": r.get("series", []) if live else [], "watching": name in PROCS or bool(r.get("fresh"))})
    feed = []
    plat_on = {x["id"]: x.get("on") for x in st.get("platforms", [])}
    for c in clips:
        cs = st["clips"].get(c["id"], {})
        stt = c["status"]
        yt = "posted" if stt == "posted" else "posting" if stt == "posting" else "queued" if stt == "approved" and plat_on.get("yt") else ""
        ig = cs.get("ig", "")
        if ig == "scheduled" and cs.get("ig_sched", 0) <= time.time():
            ig = "posted"
        if yt == "posted" and cs.get("yt_sched", 0) > time.time():
            yt = "scheduled"
        yt_note, yt_pct, ig_note = "", -1, ""
        try:
            if yt == "posting" and POST_P["cid"] == c["id"] and (HERE / "yt_progress.txt").exists():
                yt_pct = int((HERE / "yt_progress.txt").read_text(encoding="utf-8").strip() or -1)
                yt_note = "Sending video to YouTube"
            if ig == "posting" and IG_P["cid"] == c["id"] and (HERE / "ig_progress.txt").exists():
                ig_note = (HERE / "ig_progress.txt").read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            pass
        if ig == "error":
            m_ = re.search(r'"error": "([^"]{3,90})', cs.get("ig_error", "") or "")
            ig_note = m_.group(1) if m_ else "Instagram upload failed"
        if ig == "" and stt in ("approved", "posting", "posted") and plat_on.get("ig"):
            ig = "queued"
        if not (yt or ig):
            continue
        feed.append({"id": c["id"], "title": c["title"], "who": c["who"], "yt": yt, "ig": ig, "yt_id": cs.get("yt", ""), "yt_t": int(cs.get("yt_sched", 0)), "ig_t": int(cs.get("ig_sched", 0)), "yt_note": yt_note, "yt_pct": yt_pct, "ig_note": ig_note,
                     "t": max(cs.get("posted_at", 0), cs.get("ig_at", 0))})
    seen_ = {r["id"] for r in feed}
    for a_ in st.get("archive", []):
        if a_["id"] not in seen_:
            feed.append(dict(a_, yt="posted", ig="posted", yt_note="", yt_pct=-1, ig_note=""))
    order = {"posting": 0, "scheduled": 1, "queued": 2, "error": 3, "posted": 4, "": 5}
    feed.sort(key=lambda r: (min(order.get(r["yt"], 5), order.get(r["ig"], 5)), -r["t"]))
    gap = int(st.get("window", {}).get("gap", 45)) * 60
    ylog, ilog = st.get("post_log", []), st.get("ig_log", [])
    nowt = time.time()
    wait = {"yt": max(0, int(gap - (nowt - ylog[-1][0]))) if ylog else 0, "ig": max(0, int(gap - (nowt - ilog[-1][0]))) if ilog else 0}
    ext = None
    try:
        logs = [p for p in (HERE / "ig_run3_log.txt", HERE / "ig_run2_log.txt") if p.exists()]
        if logs:
            lg = max(logs, key=lambda p: p.stat().st_mtime)
            txt = lg.read_text(encoding="utf-8", errors="replace")
            posted_names = re.findall(r'\{"posted": "([^"]+)"\}', txt)
            done = "ALL DONE" in txt
            err = "STOPPED ON ERROR" in txt
            done_ig = set(json.loads((HERE / "ig_posted.json").read_text(encoding="utf-8"))) if (HERE / "ig_posted.json").exists() else set()
            left = [r["file"] for r in json.loads((HERE / "n3on_posts.json").read_text(encoding="utf-8")) if r["file"] not in done_ig]
            live = not done and not err and nowt - lg.stat().st_mtime < 1500
            ext = {"state": "error" if err else "done" if done else "running" if live else "stopped", "posted": posted_names, "left": left}
    except Exception:  # noqa: BLE001
        ext = None
    act = list(EVENTS)
    for who, r in runs.items():
        for ln in r["lines"][-3:]:
            m = re.match(r"(\d\d:\d\d):\d\d (.*)", ln)
            if m:
                act.append([m.group(1), f"{who}: {m.group(2)[:140]}"])
    act.sort(key=lambda a: a[0], reverse=True)
    def _slog():
        out_ = {}
        for d in sorted(RUNS.iterdir()) if RUNS.exists() else []:
            lg = d / "live_log.txt"
            if not (d.is_dir() and lg.exists()):
                continue
            who = re.sub(r"_\d{8}_\d{6}$", "", d.name).replace("_", " ")
            for ln in lg.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
                m = re.match(r"(\d\d:\d\d:\d\d)\s+(.*)", ln)
                if m and m.group(2).strip():
                    out_.setdefault(who, []).append([m.group(1), re.sub(r"^catch-up:\s*", "", m.group(2).strip())[:170]])
        return out_
    slog = cached("slog", 3, _slog)
    root = HERE / "runs"
    disk = shutil.disk_usage(HERE)
    used = cached("dsz", 60, lambda: dir_size(root) if root.exists() else 0)
    for c in clips:
        c.pop("_t"), c.pop("_p")
    return {"running": not st["paused"], "mode": st["mode"], "streamers": streamers, "clips": clips,
            "platforms": st["platforms"], "window": st["window"], "mbs": (HERE / "ig_mbs.txt").exists(), "accts": accts_info(), "activity": act[:80], "slog": slog, "postfeed": feed[:120], "postwait": wait, "ext": ext,
            "claude": claude_usage(),
            "health": {"streams": sum(1 for s in streamers if s["watching"]), "max": MAX_STREAMS,
                       "disk_gb": round(used / 1e9, 1), "disk_free_gb": round(disk.free / 1e9), "cpu": CPU["pct"], **RES,
                       "awake": os.name == "nt", "max_streams": MAX_STREAMS}}


def accts_info() -> dict:
    out = {}
    for k, nf in (("yt", "yt_channel.txt"), ("ig", "ig_account.txt")):
        pic = HERE / "avatars" / f"acct_{k}.jpg"
        try:
            nm = (HERE / nf).read_text(encoding="utf-8").strip() if (HERE / nf).exists() else ""
        except OSError:
            nm = ""
        out[k] = {"name": nm, "pic": pic.exists(), "v": int(pic.stat().st_mtime) if pic.exists() else 0}
    return out


AV_BUSY = [False]


def fetch_acct_avatars() -> None:
    if AV_BUSY[0]:
        return
    AV_BUSY[0] = True

    def _run():
        try:
            if (HERE / "yt_token.json").exists():
                subprocess.run([sys.executable, "-u", str(HERE / "yt_poster.py"), "avatar"], cwd=str(HERE), timeout=90,
                               capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            busy = any(x["p"] is not None and x["p"].poll() is None for x in (IG_P, IG_LOGIN, IG_MBS))
            if (HERE / "ig_account.txt").exists() and not busy:
                subprocess.run([sys.executable, "-u", str(HERE / "ig_poster.py"), "avatar"], cwd=str(HERE), timeout=120, capture_output=True,
                               env=dict(os.environ, IG_HEADLESS="1"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
        finally:
            AV_BUSY[0] = False
    threading.Thread(target=_run, daemon=True).start()


def clip_path(cid: str):
    for d in RUNS.iterdir() if RUNS.exists() else []:
        for f in clip_files(d):
            if clip_id(f) == cid:
                return f
    return None


THEMES = {"Peach": "assets/card_peach.png", "Dusk": "assets/card_dusk.png",
          "Black": "assets/card_black.png", "Grey": "assets/card_grey.png"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}


def gfont_fetch(family: str) -> dict:
    """Download a Google Font (static TTFs, regular and bold) into fonts/ so the renderer can use it."""
    import urllib.parse
    import urllib.request
    fam = re.sub(r"[^A-Za-z0-9 ]", "", family).strip()
    if not fam:
        raise ValueError("no family")
    tag = "gf_" + fam.replace(" ", "")
    have = sorted((HERE / "fonts").glob(tag + "_*.ttf"))
    if have:
        return {"ok": True, "files": [f.name for f in have], "cached": True}
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8; de-at) AppleWebKit/533.21.1 (KHTML, like Gecko) Version/5.0.5 Safari/533.21.1"}
    css = None
    for spec in (":wght@400;700", ":wght@400", ""):
        url = "https://fonts.googleapis.com/css2?family=" + urllib.parse.quote(fam).replace("%20", "+") + spec
        try:
            css = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=20).read().decode("utf-8", "replace")
            break
        except Exception:  # noqa: BLE001
            continue
    if not css:
        raise RuntimeError("Google Fonts not reachable")
    got = []
    for blk in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        u = re.search(r"url\((https://[^)]+)\)", blk)
        w = re.search(r"font-weight:\s*(\d+)", blk)
        if not u:
            continue
        wt = w.group(1) if w else "400"
        data = urllib.request.urlopen(urllib.request.Request(u.group(1), headers=hdr), timeout=30).read()
        if data[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            continue
        out = HERE / "fonts" / f"{tag}_{wt}.ttf"
        out.write_bytes(data)
        got.append(out.name)
    if not got:
        raise RuntimeError("no font files returned")
    return {"ok": True, "files": got}


AV_FAIL = {}


def avatar_file(cid: str):
    """Channel profile picture, fetched once from the public YouTube channel page and cached in avatars/."""
    if not re.fullmatch(r"UC[\w-]{20,24}", cid):
        return None
    f = HERE / "avatars" / f"{cid}.jpg"
    if f.exists():
        return f
    if time.time() - AV_FAIL.get(cid, 0) < 3600:
        return None
    try:
        import urllib.request
        hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
               "Accept-Language": "en-US,en;q=0.9"}
        page = urllib.request.urlopen(urllib.request.Request(f"https://www.youtube.com/channel/{cid}", headers=hdr),
                                      timeout=15).read().decode("utf-8", "replace")
        m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
        if not m:
            raise ValueError("no image")
        url = re.sub(r"=s\d+.*$", "=s176-c-k-c0x00ffffff-no-rj", m.group(1).replace("&amp;", "&"))
        data = urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=15).read()
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(data)
        return f
    except Exception:  # noqa: BLE001
        AV_FAIL[cid] = time.time()
        return None


REDO = {}  # clip id -> "working" | "done" | "error: ..."


def redo_clip(cid: str, side: Path) -> None:
    def go():
        REDO[cid] = "working"
        try:
            r = subprocess.run([sys.executable, "-u", "clipper.py", "--redo", str(side)], cwd=str(HERE),
                               capture_output=True, text=True, errors="replace", timeout=900)
            REDO[cid] = "done" if r.returncode == 0 else "error: " + (r.stderr or r.stdout)[-300:]
        except Exception as ex:  # noqa: BLE001
            REDO[cid] = f"error: {ex}"
    threading.Thread(target=go, daemon=True).start()


# ------------------------------------------------------------ http
def resolve_channel(link: str):
    m = re.search(r"(UC[\w-]{22})", link)
    if m:
        return m.group(1)
    url = link if link.startswith("http") else f"https://www.youtube.com/{link.lstrip('/')}"
    try:
        r = subprocess.run(["yt-dlp", *yt_extra(), "--skip-download", "--no-warnings", "--playlist-items", "1",
                            "--print", "%(channel_id)s", url], capture_output=True, text=True, timeout=60)
        m = re.search(r"(UC[\w-]{22})", r.stdout)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None




def build_report():
    """Plain-language live report of every streamer run, from the logs and files on disk."""
    import time as _t
    now = _t.time()
    base = HERE / "runs" / "live"
    dl = build_downloads()
    fmt = lambda s: (f"{int(s // 3600)}h {int(s % 3600 // 60)}m" if s >= 3600 else f"{int(s // 60)}m" if s >= 60 else f"{int(s)}s")
    out = []
    try:
        r_ = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|yt-dlp|ffmpeg' } | Select-Object -ExpandProperty CommandLine"],
                            capture_output=True, text=True, errors="replace", timeout=20)
        procs = r_.stdout or ""
    except Exception:  # noqa: BLE001
        procs = ""
    for run in sorted(base.iterdir() if base.exists() else []):
        if not run.is_dir():
            continue
        name = re.sub(r"_\d{8}_\d{6}$", "", run.name).replace("_", " ")
        alive = run.name in procs or re.search(r"\.py\s+\"?" + re.escape(name) + r"\b", procs) is not None
        lg = run / "live_log.txt"
        lines = lg.read_text(encoding="utf-8", errors="replace").splitlines() if lg.exists() else []
        last_t = lg.stat().st_mtime if lg.exists() else 0
        clips = sorted((run / "clips").glob("*.mp4")) if (run / "clips").exists() else []
        items = [x for x in dl["items"] if x["run"] == run.name]
        active = [x for x in items if x["state"] in ("downloading", "paused", "stalled")]
        if not active and now - max([last_t] + [now - x["age"] for x in items] + [0]) > 6 * 3600 and not clips:
            continue
        txt = "\n".join(lines)
        def when(pat):
            for l in reversed(lines):
                if pat in l:
                    m = re.match(r"(\d\d):(\d\d):(\d\d)", l.strip())
                    return l, m
            return None, None
        stage, detail = "Idle", ""
        if active:
            x = active[0]
            stage = {"downloading": "Downloading", "paused": "Paused", "stalled": "Download stalled"}[x["state"]]
            detail = f"{x['kind']} {x['mb'] / 1000:.2f} GB so far" + (f", {x['speed']:.2f} MB/s" if x["state"] == "downloading" and x["speed"] else "")
        elif "catch-up done" in txt or "Done." in txt:
            stage = "Finished reading"
            m = re.findall(r"catch-up done: (\d+)", txt)
            detail = f"{m[-1] if m else len(clips)} clip(s) picked"
        elif "Rendering" in txt or "Clip " in txt and "peak" in txt:
            stage, detail = "Rendering clips", ""
        elif "AI judge reading" in txt:
            stage, detail = "Claude is judging the moments", re.findall(r"AI judge reading (\d+) moments", txt)[-1] + " moments shortlisted"
        elif "Claude is reading the whole transcript" in txt:
            stage, detail = "Claude is reading the transcript to find moments", ""
        elif "Transcribing the whole video" in txt:
            stage, detail = "Speech to text (Whisper)", "the longest step for long streams"
        elif "reading it now" in txt:
            stage, detail = "Preparing to read", ""
        elif items and all(x["state"] == "done" for x in items):
            stage, detail = "Downloaded, waiting to be clipped", ""
        busy_stage = stage not in ("Idle", "Finished reading", "Downloaded, waiting to be clipped")
        if busy_stage and not alive and not active:
            stage, detail = "Stopped (not running)", "it was interrupted; ask Claude to restart it"
        if lines and stage not in ("Stopped (not running)",):
            m = re.match(r"\s*(\d\d):(\d\d):(\d\d)", lines[-1])
            if m:
                lt = _t.localtime(now)
                secs = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec) - (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]))
                if secs >= 0:
                    detail += (", " if detail else "") + f"in this step for {fmt(secs)}"
        if "Claude limit hit" in txt or "CLAUDE LIMIT" in txt:
            detail += ". Claude usage limit was hit in this run"
        errs = [l.strip()[:160] for l in lines[-40:] if re.search(r"FAILED|ERROR|(?<!\b0 )failed", l)]
        out.append(dict(streamer=name, stage=stage, detail=detail, clips=len(clips), error=errs[-1] if errs else ""))
    order = {"Downloading": 0, "Speech to text (Whisper)": 1, "Claude is reading the transcript to find moments": 1, "Claude is judging the moments": 1, "Rendering clips": 1}
    out.sort(key=lambda r: (order.get(r["stage"], 2), r["streamer"]))
    lt = _t.localtime(now)
    head = f"Live report at {lt.tm_hour:02d}:{lt.tm_min:02d}"
    busy = [r for r in out if r["stage"] not in ("Idle", "Finished reading", "Downloaded, waiting to be clipped", "Stopped (not running)")]
    summ = f"{len(busy)} job(s) running, {sum(r['clips'] for r in out)} clip(s) on disk."
    return dict(title=head, summary=summ, rows=out)


_DL_PAUSED = set()


def _dl_procs(run_name):
    """yt-dlp/ffmpeg processes writing into this run folder, plus their children."""
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
                       capture_output=True, text=True, errors="replace")
    try:
        ps = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    if isinstance(ps, dict):
        ps = [ps]
    roots = {p["ProcessId"] for p in ps if run_name in (p.get("CommandLine") or "")
             and re.match(r"(?i)(yt-dlp|ffmpeg)", p.get("Name") or "")}
    found, grow = set(roots), True
    while grow:
        grow = False
        for p in ps:
            if p["ParentProcessId"] in found and p["ProcessId"] not in found:
                found.add(p["ProcessId"])
                grow = True
    return sorted(found)


def _dl_suspend(run_name, pause):
    import ctypes
    nt = ctypes.WinDLL("ntdll")
    k32 = ctypes.WinDLL("kernel32")
    n = 0
    for pid in _dl_procs(run_name):
        h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if h:
            (nt.NtSuspendProcess if pause else nt.NtResumeProcess)(h)
            k32.CloseHandle(h)
            n += 1
    if pause:
        _DL_PAUSED.add(run_name)
    else:
        _DL_PAUSED.discard(run_name)
    return n


_DL_PREV = {}


def build_downloads():
    """Every download in runs/live: size now, speed since the last look, stalled or finished."""
    import time as _t
    now = _t.time()
    out = []
    base = HERE / "runs" / "live"
    for run in sorted(base.iterdir()) if base.exists() else []:
        if not run.is_dir():
            continue
        name = re.sub(r"_\d{8}_\d{6}$", "", run.name).replace("_", " ")
        for sub in ("catchup", "catchup/hq"):
            d = run / sub
            if not d.exists():
                continue
            for f in d.iterdir():
                if not f.is_file() or "Frag" in f.name or f.suffix in (".ytdl", ".txt", ".json"):
                    continue
                if f.suffix.lower() not in (".part", ".mp4", ".m4a", ".webm", ".mkv"):
                    continue
                st = f.stat()
                key = str(f)
                prev = _DL_PREV.get(key)
                spd = 0.0
                if prev and now > prev[0]:
                    spd = max(0.0, (st.st_size - prev[1]) / (now - prev[0]))
                if not prev or now - prev[0] > 4:
                    _DL_PREV[key] = (now, st.st_size)
                part = f.suffix == ".part"
                age = now - st.st_mtime
                state = ("paused" if run.name in _DL_PAUSED else ("downloading" if age < 90 else "stalled")) if part else "done"
                kind = "HQ window" if sub.endswith("hq") else ("audio" if ".m4a" in f.name or f.name.startswith("audio") else "stream")
                if not part and age > 6 * 3600:
                    continue
                out.append(dict(streamer=name, run=run.name, file=f.name, kind=kind, mb=round(st.st_size / 1e6, 1),
                                speed=round(spd / 1e6, 2), state=state, age=int(age)))
    for pf in base.glob("*/catchup/hq/*.dlprog.txt"):  # expected size written by remaster.py -> live percent for HQ window downloads
        try:
            tot = float(json.loads(pf.read_text(encoding="utf-8")).get("total_mb") or 0)
            stem = pf.name[:-len(".dlprog.txt")]
            its = [x for x in out if x["run"] == pf.parent.parent.parent.name and x["kind"] == "HQ window" and x["file"].startswith(stem)]
            if tot > 0 and its:
                pc_ = min(0.99, sum(x["mb"] for x in its) / tot) if any(x["state"] != "done" for x in its) else 1.0
                for x in its:
                    x["pct"] = round(pc_, 3)
        except Exception:  # noqa: BLE001
            pass
    for x in out:  # no expected size from remaster.py (older run): estimate from the finished windows of the same kind in this run
        if x["kind"] == "HQ window" and x["state"] != "done" and x.get("pct") is None:
            wide = "_wide" in x["file"]
            same = [y["mb"] for y in out if y["run"] == x["run"] and y["kind"] == "HQ window" and y["state"] == "done" and ("_wide" in y["file"]) == wide]
            if same:
                avg = sum(same) / len(same)
                x["pct"] = round(min(0.97, x["mb"] / avg), 3)
                x["approx"] = True
    logs = []
    for lg in ("fresh6_log.txt", "remaster9_log.txt", "sweep_log.txt", "livetest_log.txt", "dl_only_log.txt"):
        p = HERE / lg
        if p.exists() and now - p.stat().st_mtime < 6 * 3600:
            lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
            logs.append(dict(name=lg, mtime=int(p.stat().st_mtime), lines=[l[:240] for l in lines]))
    for run in (base.iterdir() if base.exists() else []):
        p = run / "live_log.txt"
        if p.exists() and now - p.stat().st_mtime < 6 * 3600:
            lines = [l for l in p.read_text(encoding="utf-8", errors="replace").strip().splitlines() if l.strip()][-10:]
            nm = re.sub(r"_\d{8}_\d{6}$", "", run.name).replace("_", " ")
            logs.append(dict(name=nm + " run (reading and clips)", mtime=int(p.stat().st_mtime), lines=[l[:240] for l in lines]))
    logs.sort(key=lambda x: -x["mtime"])
    return dict(now=int(now), items=out, logs=logs)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _serve(self, f, ctype):
        if not f.exists():
            return self._json({"error": "not found"}, 404)
        size = f.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    buf = fh.read(min(1 << 20, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html", "/v2"):
            b = (HERE / "ui" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif path in ("/favicon.svg", "/favicon.ico"):
            b = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="12" fill="#FFD400"/></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif path == "/api/state":
            self._json(build_state())
        elif path == "/api/report":
            self._json(build_report())
        elif path == "/api/jobs":
            if jobs_api:
                jobs_api._YTX[0] = yt_extra
            self._json(jobs_api.build(_DL_PAUSED) if jobs_api else {"now": int(time.time()), "jobs": []})
        elif path == "/jobs":
            b = (HERE / "ui" / "jobs.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif path == "/api/downloads":
            self._json(build_downloads())
        elif path == "/downloads":
            b = (HERE / "ui" / "downloads.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/words/"):
            b = (HERE / "ui" / "words.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/editor/"):
            b = (HERE / "ui" / "editor.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/api/editor/"):
            cid = path.rsplit("/", 1)[-1]
            f = clip_path(cid)
            side = f.with_suffix(".edit.json") if f else None
            if not side or not side.exists():
                return self._json({"error": "This clip has no edit data."}, 404)
            d = json.loads(side.read_text(encoding="utf-8"))
            m = re.search(r"card_(\w+)\.png", " ".join(d.get("argv", [])) + " " + str(d.get("bg", "")))
            self._json({"id": cid, "s": d["s"], "e": d["e"], "words": d["words"], "title": d.get("tweet", ""),
                        "p": d.get("p"), "status": REDO.get(cid, "idle"), "v": f.stat().st_mtime,
                        "design": True, "theme": (m.group(1).capitalize() if m else "Peach"),
                        "name": f.stem})
        elif path.startswith("/src/"):
            f = clip_path(path[5:].split(".")[0])
            side = f.with_suffix(".edit.json") if f else None
            if not side or not side.exists():
                return self._json({"error": "not found"}, 404)
            self._serve(HERE / Path(json.loads(side.read_text(encoding="utf-8"))["input"]), "video/mp4")
        elif path in ("/ui/icons.js", "/ui/islands.js"):
            self._serve(HERE / path[1:], "application/javascript")
        elif path == "/ui/gfonts.json":
            self._serve(HERE / "ui" / "gfonts.json", "application/json")
        elif path == "/api/audio":
            def ls(k):
                d = HERE / "assets" / k
                return [{"name": f.name, "size": f.stat().st_size} for f in sorted(d.iterdir())
                        if f.suffix.lower() in AUDIO_EXT] if d.is_dir() else []
            self._json({"music": ls("music"), "sfx": ls("sfx")})
        elif path.startswith("/audio/"):
            parts = unquote(path).split("/")
            f = HERE / "assets" / ("sfx" if parts[2] == "sfx" else "music") / Path(parts[-1]).name
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXT:
                return self._json({"error": "not found"}, 404)
            self._serve(f, "audio/mpeg" if f.suffix.lower() == ".mp3" else "audio/wav" if f.suffix.lower() == ".wav" else "application/octet-stream")
        elif path in ("/acct/yt", "/acct/ig"):
            f = HERE / "avatars" / f"acct_{path[-2:]}.jpg"
            if not f.exists():
                return self._json({"error": "no avatar"}, 404)
            b = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/avatar/"):
            f = avatar_file(Path(path).name)
            if not f:
                return self._json({"error": "no avatar"}, 404)
            b = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/fonts/") or path.startswith("/assets/"):
            f = HERE / path.split("/")[1] / Path(path).name
            if f.suffix.lower() not in (".ttf", ".otf", ".png") or not f.exists():
                return self._json({"error": "not found"}, 404)
            self._serve(f, "image/png" if f.suffix.lower() == ".png" else "font/ttf")
        elif path.startswith("/api/captions/"):
            cid = path.rsplit("/", 1)[-1]
            f = clip_path(cid)
            side = f.with_suffix(".edit.json") if f else None
            if not side or not side.exists():
                return self._json({"error": "This clip was made before the caption editor. New clips can be edited."}, 404)
            d = json.loads(side.read_text(encoding="utf-8"))
            ws = [dict(start=round(w["start"] - d["s"], 2), end=round(w["end"] - d["s"], 2), word=w["word"]) for w in d["words"] if w["end"] > d["s"]]
            th = next((k for k, v in THEMES.items() if v == d.get("bg")), "")
            self._json({"words": ws, "dur": round(d["e"] - d["s"], 2), "status": REDO.get(cid, "idle"),
                        "title": (d.get("tweet") or ""), "theme": th, "themes": list(THEMES)})
        elif path.startswith("/thumb/"):
            f = clip_path(path[7:].split(".")[0])
            t = thumb_for(f) if f else None
            if not t:
                return self._json({"error": "not found"}, 404)
            b = t.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif path.startswith("/clip/"):
            f = clip_path(path[6:].split(".")[0])
            if not f:
                return self._json({"error": "not found"}, 404)
            size = f.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        buf = fh.read(min(1 << 20, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        u = urlparse(self.path)
        if u.path in ("/api/downloads/pause", "/api/downloads/resume"):
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            rn = Path(str(body.get("run", ""))).name
            if not rn or not (HERE / "runs" / "live" / rn).exists():
                return self._json({"error": "Unknown download."}, 400)
            k = _dl_suspend(rn, u.path.endswith("pause"))
            return self._json({"ok": True, "processes": k})
        if u.path == "/api/bench/scout":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            subprocess.Popen([sys.executable, "-u", str(HERE / "bench_scout.py"), str(body.get("model", "qwen2.5:14b"))[:60]],
                             cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=open(HERE / "bench_scout_log.txt", "wb"), stderr=subprocess.STDOUT,
                             creationflags=(0x08000000 | 0x00000200) if sys.platform == "win32" else 0)
            return self._json({"ok": True})
        if u.path == "/api/bench/asr":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            src = str(body.get("src", ""))
            if not src.lower().endswith((".mp4", ".wav")) or ".." in src or not Path(src).exists():
                return self._json({"error": "No such video."}, 400)
            subprocess.Popen([sys.executable, "-u", str(HERE / "bench_asr.py"), src, str(int(body.get("start", 3600))), str(int(body.get("secs", 600)))],
                             cwd=str(HERE), stdin=subprocess.DEVNULL, stdout=open(HERE / "bench_asr_log.txt", "wb"), stderr=subprocess.STDOUT,
                             creationflags=(0x08000000 | 0x00000200) if sys.platform == "win32" else 0)
            return self._json({"ok": True})
        if u.path in ("/api/jobs/stop", "/api/jobs/delete-source", "/api/jobs/delete-run", "/api/jobs/pause"):
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if not jobs_api:
                return self._json({"error": "Jobs module is not loaded."}, 500)
            try:
                return self._json((jobs_api.stop if u.path.endswith("stop") else jobs_api.request_pause if u.path.endswith("pause") else jobs_api.delete_run if u.path.endswith("delete-run") else jobs_api.delete_source)(body))
            except Exception as ex:  # noqa: BLE001
                return self._json({"error": str(ex)[:200]}, 500)
        if u.path == "/api/ig/retry":
            try:
                rb_ = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                rb_ = {}
            with LOCK:
                st = load_state()
                n_ = 0
                for cid_, c in st.get("clips", {}).items():
                    if rb_.get("id") and cid_ != rb_["id"]:
                        continue
                    if c.get("ig") in ("error", "posting") and not (IG_P["p"] is not None and IG_P["p"].poll() is None):
                        c.pop("ig", None); c.pop("ig_error", None); n_ += 1
                        if c.get("status") == "posting_ig":
                            c["status"] = "approved"
                save_state(st)
            return self._json({"ok": True, "cleared": n_})
        if u.path == "/api/ig/dbg":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if IG_P["p"] is not None and IG_P["p"].poll() is None:
                return self._json({"error": "An Instagram upload is running."}, 409)
            args = [str(x) for x in body.get("args", [])]
            if not args or not (args[0] in ("probe", "check", "menu") or (args[0] == "post" and "--dry" in args)):
                return self._json({"error": "not allowed"}, 400)
            subprocess.Popen([sys.executable, "-u", str(HERE / "ig_poster.py")] + args, cwd=str(HERE), env=dict(os.environ, IG_HEADLESS="1"),
                             stdout=open(HERE / "ig_shots" / "dbg_log.txt", "w"), stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return self._json({"ok": True})
        if u.path == "/api/yt/resched":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            vid, slot = str(body.get("id", "")), int(body.get("slot", 0))
            if not vid or slot < time.time() + 600:
                return self._json({"error": "bad id or time"}, 400)
            r_ = subprocess.run([sys.executable, "-u", str(HERE / "yt_poster.py"), "resched", vid, str(slot)], cwd=str(HERE), capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if "rescheduled" in (r_.stdout or ""):
                with LOCK:
                    st = load_state()
                    for c in st["clips"].values():
                        if c.get("yt") == vid:
                            c["yt_sched"] = slot
                            c["sched_at"] = slot
                    st.setdefault("sched_last", {})["both"] = max(slot, st.get("sched_last", {}).get("both", 0))
                    save_state(st)
                return self._json({"ok": True})
            return self._json({"error": (r_.stdout + r_.stderr)[-300:]}, 500)
        if u.path == "/api/remaster":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            run_ = str(body.get("run", ""))
            picks = [str(x) for x in body.get("picks", [])][:10]
            if not re.search(r"_\d{8}_\d{6}$", run_) or ".." in run_ or "/" in run_ or "\\" in run_:
                return self._json({"error": "bad run"}, 400)
            subprocess.Popen([sys.executable, "-u", "remaster.py", run_] + picks, cwd=str(HERE), stdin=subprocess.DEVNULL,
                             stdout=open(HERE / "remaster_fix_log.txt", "wb"), stderr=subprocess.STDOUT,
                             creationflags=(0x08000000 | 0x00000200) if sys.platform == "win32" else 0)
            return self._json({"ok": True})
        if u.path == "/api/pub/avatars":
            fetch_acct_avatars()
            return self._json({"ok": True})
        if u.path == "/api/streamer/cleanup":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            nm_ = str(body.get("name", ""))
            with LOCK:
                st = load_state()
                ok_ = cleanable(st, gather_clips(st), nm_)
            if not ok_:
                return self._json({"error": "Not every clip is uploaded and scheduled yet."}, 400)
            if not jobs_api:
                return self._json({"error": "Jobs module is not loaded."}, 500)
            with LOCK:
                st = load_state()
                have_ = {a["id"] for a in st.setdefault("archive", [])}
                for x in gather_clips(st):
                    cs = st["clips"].get(x["id"], {})
                    if x["who"] == nm_ and x["id"] not in have_ and x["status"] != "rejected" and not cs.get("bin"):
                        st["archive"].append({"id": x["id"], "title": x["title"], "who": x["who"], "yt_id": cs.get("yt", ""), "yt_t": int(cs.get("yt_sched", 0)),
                                              "ig_t": int(cs.get("ig_sched", 0)), "t": max(int(cs.get("posted_at", 0) or 0), int(cs.get("ig_at", 0) or 0))})
                save_state(st)
            freed, errs = 0.0, 0
            for d in sorted(RUNS.iterdir()):
                if d.is_dir() and re.search(r"_\d{8}_\d{6}$", d.name) and re.sub(r"_\d{8}_\d{6}$", "", d.name).replace("_", " ") == nm_:
                    r_ = jobs_api.delete_run({"run": d.name, "confirm": True})
                    if r_.get("ok"):
                        freed += r_.get("freed_gb", 0)
                    else:
                        errs += 1
            return self._json({"ok": errs == 0, "freed_gb": round(freed, 2), "error": "Some files are still in use." if errs else ""})
        if u.path == "/api/ig/mbs":
            if IG_MBS["p"] is not None and IG_MBS["p"].poll() is None:
                return self._json({"ok": True, "note": "The window is already open."})
            if IG_P["p"] is not None and IG_P["p"].poll() is None:
                return self._json({"error": "An Instagram upload is running. Try again when it has finished."}, 409)
            IG_MBS["p"] = subprocess.Popen([sys.executable, "-u", str(HERE / "ig_poster.py"), "mbs"], cwd=str(HERE), stdout=open(HERE / "ig_shots" / "mbs_log.txt", "w"), stderr=subprocess.STDOUT)
            return self._json({"ok": True, "note": "A browser window opened. Click Continue with Instagram there and approve."})
        if u.path == "/api/ig/login":
            if IG_LOGIN["p"] is not None and IG_LOGIN["p"].poll() is None:
                return self._json({"ok": True, "note": "The sign-in window is already open."})
            if IG_P["p"] is not None and IG_P["p"].poll() is None:
                return self._json({"error": "An Instagram upload is running. Try again when it has finished."}, 409)
            IG_LOGIN["p"] = subprocess.Popen([sys.executable, "-u", str(HERE / "ig_poster.py"), "login", "--fresh"], cwd=str(HERE),
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._json({"ok": True, "note": "A browser window opened. Sign in there, it closes by itself."})
        if u.path == "/api/jobs/resume":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if not jobs_api:
                return self._json({"error": "Jobs module is not loaded."}, 500)
            try:
                return self._json(jobs_api.resume(body))
            except Exception as ex:  # noqa: BLE001
                return self._json({"error": str(ex)[:200]}, 500)
        if u.path == "/api/jobs/start":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if not jobs_api:
                return self._json({"error": "Jobs module is not loaded."}, 500)
            try:
                return self._json(jobs_api.start(body, CREATORS_F, yt_extra, event))
            except Exception as ex:  # noqa: BLE001
                return self._json({"error": str(ex)[:200]}, 500)
        if u.path == "/api/audio/upload":
            q = dict(x.split("=", 1) for x in u.query.split("&") if "=" in x)
            name = Path(unquote(q.get("name", ""))).name
            kind = "sfx" if q.get("kind") == "sfx" else "music"
            if Path(name).suffix.lower() not in AUDIO_EXT or not name:
                return self._json({"error": "Use a wav, mp3, m4a, ogg or flac file."}, 400)
            if n > 80 * 1024 * 1024:
                return self._json({"error": "File is over 80 MB."}, 400)
            dst = HERE / "assets" / kind
            dst.mkdir(parents=True, exist_ok=True)
            (dst / name).write_bytes(self.rfile.read(n))
            return self._json({"ok": True, "name": name})
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)
        path = urlparse(self.path).path
        if path == "/api/gfont":
            try:
                return self._json(gfont_fetch(str(d.get("family", ""))))
            except Exception as ex:  # noqa: BLE001
                return self._json({"error": str(ex)[:160]}, 502)
        if path == "/api/restart":
            if any(x["p"] is not None and x["p"].poll() is None for x in (POST_P, IG_P)):
                return self._json({"error": "An upload is running. Restart once it has finished."}, 409)
            self._json({"ok": True})

            def _restart():
                time.sleep(1.0)
                for p in list(PROCS.values()):
                    try:
                        p.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    subprocess.Popen(["wscript.exe", str(HERE / "clipdesk_hidden.vbs")], cwd=str(HERE),
                                     creationflags=0x00000008 | 0x00000200,  # detached, no window
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    time.sleep(0.5)
                    os._exit(0)
            threading.Thread(target=_restart, daemon=True).start()
            return
        st = load_state()
        if path == "/api/pause":
            st["paused"] = bool(d.get("paused"))
            event("Paused. Watching and judging stop for new streams." if st["paused"] else "Resumed.")
            if st["paused"]:
                for name, p in list(PROCS.items()):
                    p.terminate()
        elif path == "/api/clip/delete":
            gone, failed = [], []
            for cid in d.get("ids", []):
                f = clip_path(str(cid))
                if f is None:
                    st["clips"].pop(str(cid), None)
                    continue
                if REDO.get(str(cid)) == "working":
                    failed.append(str(cid))
                    continue
                for x in set(f.parent.glob(glob.escape(f.stem) + ".*")) | {f}:
                    try:
                        x.unlink()
                    except OSError:
                        pass
                if f.exists():
                    failed.append(str(cid))
                    continue
                st["clips"].pop(str(cid), None)
                REDO.pop(str(cid), None)
                gone.append(str(cid))
            if gone:
                event(f"Deleted {len(gone)} clip(s) permanently.")
            save_state(st)
            return self._json({"ok": True, "deleted": gone, "failed": failed})
        elif path == "/api/clip":
            c = st["clips"].setdefault(d["id"], {})
            if "status" in d:
                c["status"] = d["status"]
            if "bin" in d:
                c["bin"] = bool(d["bin"])
            if "title" in d:
                c["title"] = str(d["title"])[:200]
        elif path.startswith("/api/editor/"):
            cid = path.rsplit("/", 1)[-1]
            f = clip_path(cid)
            side = f.with_suffix(".edit.json") if f else None
            if not side or not side.exists():
                return self._json({"error": "This clip has no edit data."}, 404)
            if REDO.get(cid) == "working":
                return self._json({"error": "Still rendering the last save. Wait a moment."}, 400)
            sd = json.loads(side.read_text(encoding="utf-8"))
            try:
                ns, ne = max(0.0, float(d["s"])), float(d["e"])
            except (KeyError, TypeError, ValueError):
                return self._json({"error": "Bad trim."}, 400)
            if ne - ns < 5:
                return self._json({"error": "Keep at least 5 seconds."}, 400)
            sd["peak"] = sd.get("peak", sd["s"] + (sd.get("peak_rel") or 0))
            sd["s"], sd["e"] = ns, ne
            sd["words"] = [dict(start=float(w["start"]), end=float(w["end"]), word=str(w["word"]))
                           for w in d.get("words", []) if str(w.get("word", "")).strip()]
            sd["tweet"] = str(d.get("title", ""))[:140]
            if isinstance(d.get("p"), dict):
                sd["p"] = d["p"]
            side.write_text(json.dumps(sd, ensure_ascii=False), encoding="utf-8")
            redo_clip(cid, side)
            event("Re-rendering a clip from the editor.")
        elif path.startswith("/api/captions/"):
            cid = path.rsplit("/", 1)[-1]
            f = clip_path(cid)
            side = f.with_suffix(".edit.json") if f else None
            if not side or not side.exists():
                return self._json({"error": "This clip has no caption data."}, 404)
            sd = json.loads(side.read_text(encoding="utf-8"))
            sd["words"] = [dict(start=float(w["start"]) + sd["s"], end=float(w["end"]) + sd["s"], word=str(w["word"]))
                           for w in d.get("words", []) if str(w.get("word", "")).strip()]
            sd.setdefault("peak", sd["s"] + (sd.get("peak_rel") or 0))
            if "title" in d:
                sd["tweet"] = str(d["title"])[:140]
            if d.get("theme") in THEMES:
                sd["bg"] = THEMES[d["theme"]]
            try:
                ts, te = max(0.0, float(d.get("trim_start", 0))), max(0.0, float(d.get("trim_end", 0)))
            except (TypeError, ValueError):
                ts = te = 0.0
            if ts or te:
                ns, ne = sd["s"] + ts, sd["e"] - te
                if ne - ns < 8:
                    return self._json({"error": "Keep at least 8 seconds."}, 400)
                sd["s"], sd["e"] = ns, ne
            side.write_text(json.dumps(sd, ensure_ascii=False), encoding="utf-8")
            if REDO.get(cid) == "working":
                return self._json({"error": "Still saving the last edit. Wait a moment."}, 400)
            redo_clip(cid, side)
            event("Re-rendering a clip with edited captions.")
        elif path == "/api/streamer/auto":
            st["streamers"].setdefault(d["name"], {})["auto"] = bool(d["auto"])
        elif path == "/api/settings":
            for k in ("mode", "window"):
                if k in d:
                    st[k] = d[k]
            if "platforms" in d:
                st["platforms"] = d["platforms"]
        elif path == "/api/streamer/add":
            name, link = str(d.get("name", "")).strip(), str(d.get("link", "")).strip()
            if not name or not link:
                return self._json({"error": "Add a name and a channel link."}, 400)
            cid = resolve_channel(link)
            if not cid:
                return self._json({"error": "Could not find that channel. Paste the channel link or its UC id."}, 400)
            perm = {"test": "not asked yet, test only", "asked": "asked, no reply yet",
                    "approved": "approved"}.get(d.get("perm"), "not asked yet, test only")
            data = json.loads(CREATORS_F.read_text(encoding="utf-8"))
            if any(x.get("channel_id") == cid for x in data["creators"]):
                return self._json({"error": "That channel is already on the list."}, 400)
            handle = link if link.startswith("@") else ""
            data["creators"].append({"name": name, "channel_id": cid, "uploader_id": handle, "permission": perm})
            CREATORS_F.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            event(f"Added {name} to the allowlist.")
        else:
            return self._json({"error": "not found"}, 404)
        save_state(st)
        self._json({"ok": True})


def purge_rejected() -> None:
    """Rejected clips are deleted for good the next time Clipdesk starts (after a PC restart)."""
    try:
        st = load_state()
        ids = [k for k, v in st.get("clips", {}).items() if v.get("status") == "rejected"
               or (v.get("bin") and v.get("status") == "posted" and max(int(v.get("yt_sched") or 0), int(v.get("ig_sched") or 0)) <= time.time())]
        gone = 0
        for cid in ids:
            f = clip_path(cid)
            if f is not None:
                for x in set(f.parent.glob(glob.escape(f.stem) + ".*")) | {f}:
                    try:
                        x.unlink()
                    except OSError:
                        pass
                if f.exists():
                    continue
            st["clips"].pop(cid, None)
            gone += 1
        if gone:
            save_state(st)
            event(f"Deleted {gone} rejected or binned clip(s) permanently.")
    except Exception as ex:  # noqa: BLE001
        event(f"Purge error: {str(ex)[:100]}")


def main() -> None:
    purge_rejected()
    for fn in (watcher, publisher, keep_awake, cpu_loop, res_loop):
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    event("Clipdesk started. Keeping this computer awake while it runs.")
    print(f"Clipdesk running at http://127.0.0.1:{PORT}  (close this window to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in PROCS.values():
            p.terminate()


if __name__ == "__main__":
    main()
