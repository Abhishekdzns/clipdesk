"""Run one streamer end to end.  python run_streamer.py <Name> --url <youtube url or id> [--clips N] [<start_s> <len_s>]
Reads the whole stream (or only the window start_s..start_s+len_s), picks moments, remakes every clip from 1080p.
Caption language comes from creators.json (en or hi); a streamer without one is refused (CLIP RULES).
Ended streams: whole stream is read at 480p for moments, then every clip is remastered from a 1080p h264 window.
Live streams: what already happened is fetched at top quality and read. Spicy scenes (kissing, strip club, romance)
and the first reaction to the game are asked for. Clips land in runs\\live\\<Name>_<time>\\clips. Log: run_streamer_log.txt"""
import sys as _sys  # Windows pipes default to cp1252: a stray character in a log line must never stop a job
for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
import json, re, shutil, subprocess, sys, threading, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
import os
os.environ.setdefault("CLIPPER_JUDGE_MODEL", "qwen3-vl:8b-instruct")
os.environ.setdefault("CLIPPER_SCOUT_MODEL", "qwen3-vl:8b-instruct")  # same model reads the transcript; Claude only if a part fails  # free local vision judge (Ollama). Claude only scouts, titles, Hinglish.
sys.path.insert(0, str(HERE))
LOG = HERE / "run_streamer_log.txt"
TOPIC = ("live stream, GTA VI reactions. This is an adult game, so we want what pulls views: relatable funny moments, "
         "awkward or spicy scenes (Lucia, Mandy, flirting, strip club, dancers, kissing, lap dance, bad dates, rejection), epic fails, "
         "getting busted by the cops, crashes and chaos, weird or glitchy NPCs, the Cool Dog and jet ski dog, pedestrians filming you, and crazy over the top streamer reactions, plus the very first "
         "reaction to the game. Give each clip a different kind of moment.")
KEYW = (r"strip|club|kiss|kissing|sex|naked|topless|lucia|mandy|dancer|booty|thick|\bass\b|\btits?\b|\bboobs?\b|romance|girlfriend|first time|"
        r"\bcops?\b|police|busted|wanted|\bnpcs?\b|weird|cool dog|dog island|jet ?ski|alligator|lookalike|phone|recording me")
JOBS = [  # name, url, kind, clips, hindi
    ("Mizo", "hrVIm-8R0As", "ended", 2, True),
]


def say(x):
    print(x, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + str(x) + "\n")


def slug(n):
    return re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_")


QS, QL = None, None


def main():
    import clipper, live
    global QS, QL
    global TOPIC, KEYW
    _nm = next((x for x in sys.argv[1:] if not x.startswith("--") and not x.isdigit() and not x.startswith("http")), "")
    _tf, _kf = HERE / f"topic_{slug(_nm)}.txt", HERE / f"keywords_{slug(_nm)}.txt"  # per-streamer topic overrides (e.g. an Easter egg stream)
    if _tf.exists():
        TOPIC = _tf.read_text(encoding="utf-8").strip()
        say(f"topic file in use: {_tf.name}")
    if _kf.exists():
        KEYW = _kf.read_text(encoding="utf-8").strip().replace("\n", "")
        say(f"keywords file in use: {_kf.name}")
    args = sys.argv[1:]
    url_arg = args[args.index("--url") + 1] if "--url" in args else ""
    resume = args[args.index("--resume") + 1] if "--resume" in args else ""  # reuse this run folder: saved video, transcript and analysis
    nclips = int(args[args.index("--clips") + 1]) if "--clips" in args else 100  # no cap: every relevant, hooky moment
    mode_arg = args[args.index("--mode") + 1] if "--mode" in args else ""  # face = facecam + gameplay, game = gameplay only
    rest = [x for i, x in enumerate(args) if x not in ("--url", "--clips", "--resume", "--mode") and (i == 0 or args[i - 1] not in ("--url", "--clips", "--resume", "--mode"))]
    only = [x for x in rest if not x.isdigit()]
    nums = [x for x in rest if x.isdigit()]
    if len(nums) >= 2 and int(nums[1]) < 99999:
        QS, QL = int(nums[0]), int(nums[1])
    cr = {c["name"]: c for c in json.loads((HERE / "creators.json").read_text(encoding="utf-8"))["creators"]}
    global JOBS
    if only and not any(j[0] == only[0] for j in JOBS) or url_arg:
        nm0 = " ".join(only)
        c0 = cr.get(nm0, {})
        if c0.get("language") not in ("en", "hi"):
            say(f"STOP: {nm0} has no language (en or hi) in creators.json. Ask the owner.")
            return
        vid0 = url_arg.split("v=")[-1].split("&")[0] if url_arg else ""
        if not vid0:
            prev0 = sorted((HERE / "runs" / "live").glob(slug(nm0) + "_*"))
            lg = (prev0[-1] / "live_log.txt") if prev0 else None
            m0 = re.search(r"watch\?v=([\w-]+)", lg.read_text(encoding="utf-8", errors="replace")) if lg and lg.exists() else None
            vid0 = m0.group(1) if m0 else ""
        if not vid0:
            say(f"STOP: no link for {nm0}")
            return
        JOBS = [(nm0, vid0, "ended", nclips, c0.get("language") == "hi")]
        only = [nm0]
    ytx = clipper.yt_extra()
    for nm, vid, kind, top, hi in JOBS:
        if only and nm not in only:
            continue
        try:
            c = cr.get(nm, {})
            url = f"https://www.youtube.com/watch?v={vid}"
            prev = sorted((HERE / "runs" / "live").glob(slug(nm) + "_*"))
            run = (HERE / "runs" / "live" / resume) if (resume and (HERE / "runs" / "live" / resume).is_dir()) else \
                (prev[-1] if (prev and not url_arg) else HERE / "runs" / "live" / f"{slug(nm)}_{time.strftime('%Y%m%d_%H%M%S')}")
            cd = run / "catchup"
            (run / "clips").mkdir(parents=True, exist_ok=True)
            cd.mkdir(exist_ok=True)
            if resume and (run / "live_log.txt").exists():
                with open(run / "live_log.txt", "a", encoding="utf-8") as lf_:
                    lf_.write(time.strftime("%H:%M:%S ") + "      resumed from the saved video, transcript and analysis\n")
            else:
                (run / "live_log.txt").write_text(f"Live mode on {url}\n", encoding="utf-8")
            mf = run / "mode.txt"
            mode = mode_arg if mode_arg in ("face", "game") else (mf.read_text().strip() if mf.exists() else "face")
            if mode not in ("face", "game"):
                mode = "face"
            mf.write_text(mode)
            say(f"== {nm}: start ({kind}, {top} clips, {'gameplay only' if mode == 'game' else 'facecam + gameplay'})")
            argv = ["live.py", "--name", nm, "--handle", (c.get("uploader_id") or "").lstrip("@") or nm.replace(" ", ""),
                    "--brain-topic", TOPIC, "--catchup-top", str(top), "--layout", "title_card_game" if mode == "game" else "title_card", "--url-unused"]
            argv.remove("--url-unused")
            if hi:
                argv += ["--hinglish"]
            sect = []
            if kind == "live":
                st = live.stream_start(url)
                if st:
                    sect = ["--download-sections", f"*0-{min(9000, max(600, int(time.time() - st) - 120))}"]
                    say(f"{nm}: live for {int((time.time() - st) // 60)} min, reading minutes 0 to {min(9000, max(600, int(time.time() - st) - 120)) // 60}")
            t0 = time.time()
            vid_only = next(iter(cd.glob("past.f*.mp4")), None)
            if not (cd / "past.mp4").exists() and vid_only:
                say(f"{nm}: video already here ({vid_only.stat().st_size // 1_000_000} MB), getting the audio only")
                for p_ in cd.glob("past.f*.m4a*"):
                    p_.unlink(missing_ok=True)
                aud = None
                for k in range(4):
                    ra = subprocess.run(["yt-dlp", *ytx, "-N", "4", "-f", "140/251/ba", "--retries", "20", "--fragment-retries", "50",
                                         "-o", str(cd / "audio.%(ext)s"), url], capture_output=True, text=True, encoding="utf-8", errors="replace")
                    aud = next((x for x in cd.glob("audio.*") if x.suffix not in (".part", ".ytdl") and "Frag" not in x.name), None)
                    if aud:
                        break
                    say(f"{nm}: audio try {k + 1} failed: {(ra.stdout + ra.stderr)[-200:]}")
                    time.sleep(60)
                if aud:
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(vid_only), "-i", str(aud), "-map", "0:v:0", "-map", "1:a:0",
                                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(cd / "past.mp4")])
                    say(f"{nm}: audio merged")
            say(f"{nm}: downloading at 480p for reading (fast mode)")
            r = subprocess.CompletedProcess([], 0, "", "") if (cd / "past.mp4").exists() else subprocess.run(["yt-dlp", *ytx, "-N", "8", *sect, "-f", "bv*[height<=480][vcodec^=avc1]+ba/bv*[height<=480]+ba/b[height<=480]/b",
                                "--merge-output-format", "mp4", "--retries", "10", "--fragment-retries", "30",
                                "-o", str(cd / "past.%(ext)s"), url], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            past = cd / "past.mp4"
            if not past.exists():
                say(f"{nm}: download failed: {(r.stdout + r.stderr)[-300:]}")
                continue
            (cd / "offset.txt").write_text("0", encoding="utf-8")
            say(f"{nm}: downloaded {past.stat().st_size // 1_000_000} MB in {int(time.time() - t0)}s, reading it")
            if QS is None:
                (cd / "offset_exact.txt").write_text("0", encoding="utf-8")
            if QS is not None:
                st_, ln_ = int(QS), int(QL)
                short = cd / f"past_{st_ // 60}to{(st_ + ln_) // 60}.mp4"
                if not short.exists():
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(st_), "-t", str(ln_), "-i", str(past), "-c", "copy", str(short)])
                (cd / "offset_exact.txt").write_text(str(st_), encoding="utf-8")
                say(f"{nm}: quick mode, reading minutes {st_ // 60} to {(st_ + ln_) // 60}")
                past = short
            argv += ["--test-past", str(past), "--extra", "--gta", "--keywords", KEYW, "--judge-max", "60"]
            sys.argv = argv
            a = live.parse()
            live.LOGF = str(run / "live_log.txt")
            made = []
            live.catchup(a, run, threading.Event(), made)
            say(f"{nm}: first pass made {len(made)} clips")
            if made:
                say(f"{nm}: re-making every clip from a 1080p window")
                subprocess.run([sys.executable, "-u", "remaster.py", run.name], cwd=str(HERE))
            say(f"== {nm}: DONE")
            for m in sorted((run / "clips").glob("*.mp4")):
                say("   clip: " + m.name)
        except Exception as ex:  # noqa: BLE001
            say(f"== {nm}: FAILED {str(ex)[:300]}")
    say("ALL DONE")


main()
