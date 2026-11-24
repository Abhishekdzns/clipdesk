"""Check every clip in a folder against CLIP_RULES.md. python qa_rules.py <clips folder> [<clips folder> ...]
Writes qa_report.txt next to this file and prints PASS/FAIL per clip."""
import json, re, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
NAMES = {"Kai Cenat Live": "Kai Cenat", "Carryislive": "CarryMinati", "IShowSpeed": "Speed", "N3ON": "N3ON", "Mizo": "Mizo"}


def creators():
    try:
        return {c["name"]: c for c in json.loads((HERE / "creators.json").read_text(encoding="utf-8"))["creators"]}
    except Exception:  # noqa: BLE001
        return {}


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate,codec_name",
                        "-of", "json", str(p)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)["streams"][0]
    except Exception:  # noqa: BLE001
        return {}


def channel_of(p: Path):
    for par in p.parents:
        m = re.match(r"(.+)_\d{8}_\d{6}$", par.name)
        if m:
            return m.group(1).replace("_", " ")
    return ""


def check(mp4: Path, cr):
    bad, ch = [], channel_of(mp4)
    c = cr.get(ch, {})
    lang = c.get("language")
    if lang not in ("en", "hi"):
        bad.append(f"no language set for '{ch}' in creators.json")
    v = probe(mp4)
    if (v.get("width"), v.get("height")) != (1080, 1920):
        bad.append(f"size {v.get('width')}x{v.get('height')} (needs 1080x1920)")
    if v.get("r_frame_rate") not in ("60/1", "60000/1001"):
        bad.append(f"fps {v.get('r_frame_rate')} (needs 60)")
    if v.get("codec_name") != "h264":
        bad.append(f"codec {v.get('codec_name')} (needs h264)")
    side = mp4.with_suffix(".edit.json")
    d = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    if not d:
        bad.append("no .edit.json sidecar")
    title = d.get("title") or d.get("tweet") or ""
    name = NAMES.get(ch, ch)
    if not title.lower().startswith(name.lower()):
        bad.append(f"title does not start with {name}: '{title}'")
    if re.match(r"(?i)\s*the streamer", title):
        bad.append("generic 'The streamer' title")
    if len(title) > 40:
        bad.append(f"title too long ({len(title)} chars)")
    slug = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f,;\[\]=%$&#@!^~`{}()]', "", title.replace("'", "\u2019"))).strip(" .")[:80]
    if not mp4.stem.startswith(slug.strip(" .")[:30]):
        bad.append(f"file name is not the title ('{mp4.stem}')")
    argv = d.get("argv", [])
    lay = argv[argv.index("--layout") + 1] if "--layout" in argv else ""
    want = c.get("layout") or "title_card"
    if lay != want:
        bad.append(f"layout '{lay}' (needs '{want}')")
    if "--facecam" not in argv:
        bad.append("no facecam box")
    if lang == "en" and ("--hinglish" in argv or "--language" in argv):
        bad.append("Hindi/Hinglish flag on an English streamer")
    if lang == "hi" and "--hinglish" not in argv:
        bad.append("Hindi streamer without --hinglish")
    src = Path(d.get("input", ""))
    if src.exists():
        sv = probe(src)
        if (sv.get("height") or 0) < 1080:
            bad.append(f"rendered from a {sv.get('height')}p source (needs 1080p or more)")
    ass = mp4.with_suffix(".ass")
    if ass.exists():
        t = ass.read_text(encoding="utf-8", errors="replace")
        if lang == "en" and re.search(r"[ऀ-ॿ]", t):
            bad.append("Hindi letters in English captions")
        if lang == "hi" and len(re.findall(r"[ऀ-ॿ]", t)) > 5:
            bad.append("Devanagari in captions (needs Hinglish in English letters)")
        if "GTAArtDeco" not in t:
            bad.append("caption/title font is not GTAArtDeco")
        m = re.search(r"Style: Cap,[^\n]*", t)
        if m and not m.group(0).rstrip().endswith(",380,1") and ",380," not in m.group(0):
            bad.append("captions not at caption_y 380")
    else:
        bad.append("no .ass caption file")
    try:
        import layouts
        spec = layouts.load_layouts(str(HERE / "layouts"))[0].get(want, {})
        if not spec.get("sharpen"):
            bad.append(f"layout '{want}' has no gameplay sharpen")
        if int(spec.get("caption_y", 0)) != 380:
            bad.append(f"layout '{want}' caption_y is {spec.get('caption_y')}")
        pill = spec.get("title", {}).get("pill", {})
        if (pill.get("pad_x"), pill.get("radius")) != (30, 12):
            bad.append(f"pill padding/radius {pill.get('pad_x')}/{pill.get('radius')} (needs 30/12)")
    except Exception as ex:  # noqa: BLE001
        bad.append(f"could not read layouts ({str(ex)[:60]})")
    return bad


if __name__ == "__main__":
    cr = creators()
    out = []
    folders = [Path(x) for x in sys.argv[1:]] or [HERE / "runs"]
    clips = sorted(p for f in folders for p in f.rglob("*.mp4") if "_archive" not in str(p) and "_old" not in p.parts
                   and "cuts" not in p.parts and "buffer" not in p.parts and "catchup" not in p.parts)
    npass = 0
    for p in clips:
        bad = check(p, cr)
        npass += not bad
        line = ("PASS  " if not bad else "FAIL  ") + str(p.relative_to(HERE) if HERE in p.parents else p)
        out.append(line + "".join("\n      - " + b for b in bad))
        print(out[-1], flush=True)
    out.append(f"\n{npass}/{len(clips)} clips pass every rule")
    print(out[-1])
    (HERE / "qa_report.txt").write_text("\n".join(out) + "\n", encoding="utf-8")
