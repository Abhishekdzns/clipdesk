"""Remaster every clip: max quality source (re-downloaded around the clip), the title_card layout, a hooky short
title that is also the file name, and an info line saying where in the stream the clip comes from.
  python remaster.py [folder-substring]        all clips (skips ones already done)
  python remaster.py --finish <sidecar.json>   one new clip, no re-download
The old files go to a _old folder next to them, nothing is deleted."""
import json, os, re, shutil, subprocess, sys, time
from pathlib import Path
import numpy as np
import brain
import clipper

HERE = Path(__file__).resolve().parent
LOG = HERE / "remaster9_log.txt"
NAMES = {"Kai Cenat Live": "Kai Cenat", "Carryislive": "CarryMinati", "IShowSpeed": "Speed", "N3ON": "N3ON", "Mizo": "Mizo"}


def hindi(ch):
    if ch == "Mizo":
        return True
    try:
        cr = json.loads((HERE / "creators.json").read_text(encoding="utf-8"))["creators"]
        return any(c.get("name") == ch and c.get("language") == "hi" for c in cr)
    except Exception:  # noqa: BLE001
        return False


def name_title(desc, name, maxlen=36):
    t = re.split(r",? then |\. |; |, and |, but |, while ", (desc or "").strip())[0].strip(" .")
    t = re.sub(r"(?i)^the streamer\b", name, t)
    t = re.sub(r"(?i)\bthe streamer's\b", name + "'s", t)
    t = re.sub(r"(?i)\bthe streamer\b", name, t)
    if not t.lower().startswith(name.lower()):
        t = name + ": " + t
    if len(t) > maxlen:
        t = t[:maxlen].rsplit(" ", 1)[0]
    for _ in range(3):
        t = re.sub(r"(?i)\s+(over|and|to|a|an|the|of|in|on|for|with|at|his|her|by|from|gets|is|into)$", "", t)
    return t or name + " clip"


def override(stem):
    try:
        m = json.loads((HERE / "titles2.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    sl = stem.lower()
    for k, v in m.items():
        if k.lower() in sl:
            return v
    return ""


def qa(out: Path, title, nside: Path, ch):
    """CLIP RULES check, logged after every render."""
    bad = []
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    if r.stdout.strip().split(",")[:2] != ["1080", "1920"]:
        bad.append("size " + r.stdout.strip())
    if len(title) > 40:
        bad.append("title long")
    if re.match(r"(?i)the streamer", title):
        bad.append("generic title")
    ass = out.with_suffix(".ass")
    if ass.exists():
        t = ass.read_text(encoding="utf-8", errors="replace")
        if re.search(r"[\u0900-\u097f]", t) and not hindi(ch):
            bad.append("hindi in captions")
        if "GTAArtDeco" not in t:
            bad.append("font")
    d = json.loads(nside.read_text(encoding="utf-8"))
    if not d.get("hq"):
        bad.append("not HQ source")
    if "--facecam" not in d.get("argv", []):
        bad.append("no facecam box")
    if "--hinglish" in d.get("argv", []) and not hindi(ch):
        bad.append("hinglish flag")
    say("      QA " + ("OK" if not bad else "FLAGS: " + ", ".join(bad)))


def say(x):
    print(x, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + str(x) + "\n")


def slug(t):
    t = (t or "").replace("'", "\u2019")
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f,;\[\]=%$&#@!^~`{}()]', "", t).strip(" .")
    t = re.sub(r"\s+", " ", t)
    return t[:80].strip(" .") or "clip"


def hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def run_of(p: Path):
    for par in [p] + list(p.parents):
        if re.search(r"_\d{8}_\d{6}$", par.name):
            return par
    return None


def run_meta(run: Path):
    url, off, exact = None, None, False
    log = run / "live_log.txt"
    if log.exists():
        t = log.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Live mode on (\S+)", t)
        url = m.group(1) if m else None
        m = re.search(r"fetching minutes (\d+) to", t)
        if m:
            off = int(m.group(1)) * 60 + 30
    f = run / "catchup" / "offset.txt"
    if f.exists():
        try:
            off, exact = int(f.read_text().strip()), True
        except ValueError:
            pass
    return url, off, exact


def env(path, ss=None, t=None):
    cmd = ["ffmpeg", "-v", "error"] + (["-ss", f"{ss:.2f}"] if ss is not None else []) + ["-i", str(path)] + \
          (["-t", f"{t:.2f}"] if t else []) + ["-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    x = np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.int16).astype(np.float32)
    n = len(x) // 80
    e = np.log1p(np.abs(x[:n * 80]).reshape(n, 80).mean(1))
    return e - e.mean()


def locate(small, big):
    if len(small) < 500 or len(big) <= len(small):
        return None, 0.0
    f = 1 << (len(big) + len(small) - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(big, f) * np.conj(np.fft.rfft(small, f)), f)[:len(big) - len(small) + 1]
    cs = np.cumsum(np.r_[0, big ** 2])
    en = np.sqrt(cs[len(small):len(small) + len(c)] - cs[:len(c)]) + 1e-6
    sc = c / en / (np.linalg.norm(small) + 1e-6)
    i = int(np.argmax(sc))
    return i / 100.0, float(sc[i])


def video_gaps(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    p = []
    for x in r.stdout.split():
        try:
            p.append(float(x.strip(" ,")))
        except ValueError:
            pass
    p.sort()
    return sum(1 for a, b in zip(p, p[1:]) if b - a > 0.5)


def download(url, a, b, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    for strict in (True, False):
        for old in dest.parent.glob(dest.stem + ".*"):
            old.unlink()
        try:  # expected size for the live progress bar: window seconds x the chosen format's bitrate
            pr = subprocess.run(["yt-dlp", *clipper.yt_extra(), "--skip-download", "--no-warnings", "-f", "bv*[vcodec^=avc1]+ba/bv*[vcodec^=avc1]/bv*+ba/b",
                                 "--print", "%(tbr)s", url], capture_output=True, text=True, errors="replace", timeout=60)
            tbr = float((pr.stdout or "0").strip().splitlines()[-1])
            (dest.parent / (dest.stem + ".dlprog.txt")).write_text(json.dumps({"total_mb": round((b - a) * tbr / 8 / 1000 * 1.04, 1)}), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        cmd = ["yt-dlp", *clipper.yt_extra(), "-N", "8", "--download-sections", f"*{int(a)}-{int(b)}", "-f", "bv*[vcodec^=avc1]+ba/bv*[vcodec^=avc1]/bv*+ba/b",
               "--merge-output-format", "mp4", "--retries", "10", "--fragment-retries", "30"]
        if strict:
            cmd.append("--abort-on-unavailable-fragments")
        cmd += ["-o", str(dest.with_suffix(".%(ext)s")), url]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        f = dest if dest.exists() else next(iter(dest.parent.glob(dest.stem + ".*")), None)
        if r.returncode == 0 and f:
            return f, strict
        say("      download " + ("strict" if strict else "lax") + " failed: " + (r.stderr or "")[-200:].replace("\n", " "))
    return None, False


def probe_window(url, a, b, dest: Path):
    for old in dest.parent.glob(dest.stem + ".*"):
        old.unlink()
    r = subprocess.run(["yt-dlp", *clipper.yt_extra(), "--download-sections", f"*{int(a)}-{int(b)}", "-f", "ba/wa/w",
                        "--retries", "10", "--fragment-retries", "30", "-o", str(dest.with_suffix(".%(ext)s")), url],
                       capture_output=True, text=True, errors="replace")
    return next((x for x in dest.parent.glob(dest.stem + ".*") if x.suffix not in (".part", ".ytdl")), None) if r.returncode == 0 else None


def stream_offset(run: Path, url, old_src: Path, est):
    """Second of the stream (as the video page counts it) where past.mp4 starts. The live timeline and the finished
    video can differ by minutes, so listen to a few one-minute samples of the finished video and find where
    they sit inside past.mp4."""
    cd = run / "catchup"
    f = cd / "offset_exact.txt"
    if f.exists():
        try:
            return float(f.read_text().strip())
        except ValueError:
            pass
    if est is None:
        return None
    big = env(old_src)
    span = len(big) / 100.0
    step = max(60.0, span - 90)
    for k in (0, 1, -1, 2, -2, 3, -3, 4, -4):
        t0 = est + k * step + 30
        if t0 < 0:
            continue
        g = probe_window(url, t0, t0 + 60, cd / "probe")
        if g is None:
            continue
        pos, sc = locate(env(g), big)
        if pos is not None and sc > 0.7:
            off = t0 - pos
            f.write_text(f"{off:.2f}")
            say(f"      recording starts at {hms(off)} of the stream (match {sc:.2f}, tried {abs(k) * 2 + (1 if k else 0)} samples)")
            return off
    say("      could not place this recording inside the stream")
    return None


def retire(files, folder: Path):
    old = folder / "_old"
    old.mkdir(exist_ok=True)
    for f in files:
        if f.exists():
            dst = old / f.name
            if dst.exists():
                dst = old / (f.stem + time.strftime("_%H%M%S") + f.suffix)
            shutil.move(str(f), str(dst))


def rejected(side_path: Path) -> bool:
    """A clip you rejected (or binned) in Clipdesk is never remastered or reframed: rejecting is the manual stop."""
    try:
        import hashlib
        mp4 = side_path.with_name(side_path.name[:-10] + ".mp4")
        cid = hashlib.md5(str(mp4.relative_to(HERE)).encode()).hexdigest()[:10]
        v = json.loads((HERE / "clipdesk_state.json").read_text(encoding="utf-8")).get("clips", {}).get(cid, {})
        return v.get("status") == "rejected" or bool(v.get("bin"))
    except Exception:  # noqa: BLE001
        return False


def process(side_path: Path, download_hq=True, force_off=None):
    if rejected(side_path):
        return "skipped, rejected"
    d = json.loads(side_path.read_text(encoding="utf-8"))
    if d.get("hq") and d.get("v8"):
        return "done already"
    if d.get("hq") and d.get("v6") and not d.get("v8"):  # finished with the old style: only re-render with the emoji/pill rules
        d["v8"] = True
        side_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        ch0 = re.sub(r"_\d{8}_\d{6}$", "", (run_of(side_path) or side_path).name).replace("_", " ")
        r = subprocess.run([sys.executable, "-u", "clipper.py", "--redo", str(side_path)], cwd=str(HERE),
                           env=dict(os.environ, CLIPPER_LANG=("hi" if hindi(ch0) else "en")), capture_output=True, text=True, errors="replace")
        if r.returncode:
            return "render failed " + (r.stderr or "")[-200:]
        qa(side_path.with_name(side_path.name[:-10] + ".mp4"), d.get("title", ""), side_path, ch0)
        return "ok re-rendered " + side_path.stem
    old_src, s, e = Path(d["input"]), float(d["s"]), float(d["e"])
    run = run_of(old_src) or run_of(side_path)
    ch = re.sub(r"_\d{8}_\d{6}$", "", run.name).replace("_", " ") if run else ""
    url, off, exact = run_meta(run) if run else (None, None, False)
    model = "claude:sonnet"
    if "--brain-model" in d["argv"]:
        model = d["argv"][d["argv"].index("--brain-model") + 1]
    words = [dict(w) for w in d["words"]]
    text = " ".join(w["word"] for w in words if s <= w["start"] < e)
    name = NAMES.get(ch, ch)
    title = ""
    try:
        title = "" if override(side_path.stem) else (brain.hook_title(model, text, d.get("desc") or d.get("tweet", ""), name) or "")
    except Exception:  # noqa: BLE001
        title = ""
    ov = override(side_path.stem)
    if ov:
        title = ov
    if not title or re.match(r"(?i)\s*the streamer", title):
        title = name_title(d.get("desc") or d.get("tweet") or "", name)
    title = re.sub(r"\s+", " ", title).strip(" .,!")
    if not title.lower().startswith(name.lower()):  # CLIP RULES: the title starts with the streamer's name
        t2 = re.sub(r"(?i)^(he|she|they|streamer|the streamer)\b\s*", "", title)
        title = f"{name} {t2[:1].lower() + t2[1:]}" if t2 else name
    if len(title) > 36:  # CLIP RULES: short, one line
        cut = title[:36].rsplit(" ", 1)[0].rstrip(" ,.")
        title = re.sub(r"(?i)\s+(and|to|a|an|the|of|in|on|for|with|at|his|her|by|from|is|into|over)$", "", cut)
    say(f"      title: {title}")

    new_src, hq, a0 = old_src, bool(d.get("hq")), None
    xoff = None
    if url and old_src.name.startswith("past") and old_src.exists():
        if download_hq and force_off is not None:
            xoff = float(force_off)  # catch-up chunk: we know exactly where it starts in the stream
        elif download_hq:
            xoff = stream_offset(run, url, old_src, off)
        elif exact:
            xoff = float(off)
    if xoff is not None and download_hq:
        abs_s = xoff + s
        a0, b0 = max(0, abs_s - 80), abs_s + (e - s) + 80
        dest = run / "catchup" / "hq" / (slug(title) + ".mp4")
        f, strict = download(url, a0, b0, dest)
        if f:
            small = env(old_src, ss=s, t=min(e - s, 40.0))
            pos, score = locate(small, env(f))
            if pos is not None and score > 0.45 and abs(pos - (abs_s - a0)) < 85:
                shift = pos - s
                new_src, hq = f, True
                s, e = pos, pos + (e - s)
                for w in words:
                    w["start"] += shift
                    w["end"] += shift
                say(f"      HQ source ok: align {score:.2f}, video gaps {video_gaps(f)}, strict={strict}")
            else:
                say(f"      HQ aligned poorly (score {score:.2f}), trying a wider window")
                a1, b1 = max(0, abs_s - 420), abs_s + (e - s) + 420
                f2, strict2 = download(url, a1, b1, run / "catchup" / "hq" / (slug(title) + "_wide.mp4"))
                ok2 = False
                if f2:
                    pos2, score2 = locate(small, env(f2))
                    if pos2 is not None and score2 > 0.45 and abs(pos2 - (abs_s - a1)) < 415:
                        shift = pos2 - s
                        new_src, hq = f2, True
                        s, e = pos2, pos2 + (e - s)
                        for w in words:
                            w["start"] += shift
                            w["end"] += shift
                        say(f"      HQ source ok (wide window): align {score2:.2f}, video gaps {video_gaps(f2)}, strict={strict2}")
                        ok2 = True
                    else:
                        score = max(score, score2)
                if not ok2:
                    say(f"      HQ downloaded but could not align (score {score:.2f}), keeping the old source")
        else:
            say("      HQ download failed, keeping the old source")
    if not old_src.name.startswith("past") and d.get("src_from") is not None:
        src_from, exact = float(d["src_from"]), True
    elif xoff is not None:
        src_from = xoff + float(d["s"])
        exact = True
    elif off is not None:
        src_from = off + float(d["s"])
    else:
        src_from = None
    src_to = src_from + (float(d["e"]) - float(d["s"])) if src_from is not None else None
    info = ""
    if src_from is not None:
        info = f"From {hms(src_from)} to {hms(src_to)} of the stream" + ("" if exact else " (about)")
        if url:
            info += f" | {url}"

    argv = clipper.restyle_argv(d, "title_card")
    if not hindi(ch):  # English streamer: never Hindi captions
        argv = [x for x in argv if x != "--hinglish"]
        if "--language" in argv:
            k = argv.index("--language")
            del argv[k:k + 2]
    ow = clipper.probe(old_src)
    nw = clipper.probe(new_src)
    if "--facecam" in argv and ow["w"] and nw["w"] != ow["w"]:
        k = argv.index("--facecam") + 1
        try:
            v = [float(x) for x in argv[k].split(":")]
            if max(v) > 1:
                r = nw["w"] / ow["w"]
                argv[k] = ":".join(str(int(round(x * r))) for x in v)
        except ValueError:
            pass
    argv[0] = str(new_src)
    import facecam
    cre = next((c for c in json.loads((HERE / "creators.json").read_text(encoding="utf-8"))["creators"] if c.get("name") == ch), {})
    if cre.get("facecam_fixed"):  # CLIP RULES: a saved box for streamers the finder misses, checked for a face first
        ref = float(cre.get("facecam_ref_w") or nw["w"])
        bx = [int(round(float(v) * nw["w"] / ref)) for v in str(cre["facecam_fixed"]).split(":")]
        box = ":".join(str(v // 2 * 2) for v in bx)
        try:
            frac = facecam.verify(new_src, s, e, nw["w"], nw["h"], bx)
        except Exception as ex:  # noqa: BLE001
            frac = -1.0
            say(f"      facecam check failed to run ({str(ex)[:100]})")
        if frac < 0.5:
            say(f"      STOP: saved facecam box {box} has a face in only {max(frac, 0):.0%} of frames. Ask the owner (camera moved?).")
            return "stopped: facecam check failed, ask the owner"
        if "--facecam" in argv:
            argv[argv.index("--facecam") + 1] = box
        else:
            argv += ["--facecam", box]
        say(f"      saved facecam box {box} (face in {frac:.0%} of frames)")
    else:
        try:  # the facecam moves and resizes during a stream: find it inside this clip, on the HQ source
            box = facecam.find(new_src, s, e, nw["w"], nw["h"], side=cre.get("facecam_side") or "left", log=say)
        except Exception as ex:  # noqa: BLE001
            box = ""
            say(f"      facecam finder failed ({str(ex)[:100]})")
        if not box:
            say("      STOP: no facecam found in this clip. Ask the owner to save a facecam box for this streamer.")
            return "stopped: no facecam, ask the owner"
        if "--facecam" in argv:
            argv[argv.index("--facecam") + 1] = box
        else:
            argv += ["--facecam", box]
        say(f"      facecam box {box}")
    base = slug(title)
    clips_dir = (run / "clips") if run else side_path.parent
    clips_dir.mkdir(exist_ok=True)
    n, b2 = 2, base
    while (clips_dir / (b2 + ".mp4")).exists() and clips_dir / (b2 + ".edit.json") != side_path.with_name(b2 + ".edit.json"):
        b2 = f"{base} ({n})"
        n += 1
    base = b2
    old_mp4 = side_path.with_name(side_path.name[:-len(".edit.json")] + ".mp4")
    nd = dict(d, input=str(new_src), s=s, e=e, words=words, tweet=title, title=title, base=base, out=str(clips_dir),
              argv=argv, hq=hq, hq_final=False, v2=True, v4=True, v6=True, v8=True, title_hook=title, info=info, src_from=src_from, src_to=src_to, src_url=url or "",
              desc=d.get("desc") or d.get("tweet", ""))
    nd["peak"] = nd.get("peak", s) + (s - float(d["s"])) if "peak" in d else s
    nside = clips_dir / (base + ".edit.json")
    nside.write_text(json.dumps(nd, ensure_ascii=False), encoding="utf-8")
    env2 = dict(os.environ, CLIPPER_LANG=("hi" if hindi(ch) else "en"))
    r = subprocess.run([sys.executable, "-u", "-c", "import clipper,sys; clipper.resub(sys.argv[1])", str(nside)],
                       cwd=str(HERE), capture_output=True, text=True, errors="replace", env=env2)
    out = clips_dir / (base + ".mp4")
    if r.returncode or not out.exists():
        txt = (r.stderr or "") + "\n" + (r.stdout or "")
        keep = [l.strip()[:220] for l in txt.splitlines() if any(k in l for k in ("rror", "nvalid", "Cannot", "No such", "not found", "Unable", "Traceback", "Exception")) and len(l) < 400]
        say("      render FAILED " + " || ".join(keep[-6:]))
        say("      DETAIL " + txt[-1800:].replace("\n", " ~ "))
        nside.unlink(missing_ok=True)
        return "render failed"
    (clips_dir / (base + ".info.txt")).write_text(f"{title}\n{info}\n", encoding="utf-8")
    qa(out, title, nside, ch)
    if side_path != nside:
        retire([old_mp4, old_mp4.with_suffix(".ass"), side_path], side_path.parent)
    return "ok " + out.name


def jobs():
    seen, out, extra = set(), [], []
    for run in sorted((HERE / "runs" / "live").iterdir()):
        cand = sorted((run / "clips").glob("*.edit.json")) if (run / "clips").exists() else []
        cu = sorted((run / "catchup").glob("*/clip_*.edit.json")) if (run / "catchup").exists() else []
        for p in cand:
            if p.with_name(p.name[:-10] + ".mp4").exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                seen.add((run.name, round(float(d["s"]) / 4)))
                out.append(p)
        for p in cu:
            if not p.with_name(p.name[:-10] + ".mp4").exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            key = (run.name, round(float(d["s"]) / 4))
            if key in seen:
                extra.append(p)  # same moment already covered by a clip in clips/
            else:
                seen.add(key)
                out.append(p)
    return out, extra


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--hq":  # catch-up clip from a 480p chunk: remake it from a 1080p window
        say("hq " + sys.argv[2])
        say("   " + process(Path(sys.argv[2]), download_hq=True, force_off=float(sys.argv[3])))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--finish":
        say("finish " + sys.argv[2])
        say("   " + process(Path(sys.argv[2]), download_hq=False))
        sys.exit(0)
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    picks = [x.lower() for x in sys.argv[2:]]  # optional: only clips whose name has one of these words
    todo, extra = jobs()
    todo = [p for p in todo if flt in str(p) and (not picks or any(k in p.name.lower() for k in picks))]
    extra = []
    say(f"== remaster: {len(todo)} clips, {len(extra)} duplicate test copies to retire")
    for p in extra:
        if flt in str(p):
            retire([p.with_name(p.name[:-10] + ".mp4"), p.with_name(p.name[:-10] + ".ass"), p], p.parent)
    ok = 0
    for i, p in enumerate(todo, 1):
        t0 = time.time()
        say(f"[{i}/{len(todo)}] {p.parent.parent.name}/{p.stem}")
        try:
            res = process(p)
        except Exception as ex:  # noqa: BLE001
            res = f"ERROR {ex!r}"
        say(f"   {res}  ({time.time() - t0:.0f}s)")
        ok += res.startswith(("ok", "done"))
    say(f"DONE {ok}/{len(todo)}")
