"""Post titles, captions and tags for Clipdesk clips. No AI calls, no cost: it reads what is already inside the clip
(title, the written description, the hook, the spoken words) and builds the platform text from that.

Rules (owner, 24 Nov 2026):
  1. "GTA 6" is always in the post title and caption (YouTube title, Instagram first line).
  2. Tags come from what happens in the video: names (Dexter, Satan, Bigfoot), GTA 6 things (six stars, wanted level,
     jet ski, helicopter) and the main objects of the title (train, truck, whale). Never generic filler.
  3. First hashtags are always #GTA6 #GTAVI, then the streamer, then up to 4 topic tags, then the platform tag.
The on-video title stays exactly as CLIP_RULES.md says. Only the text sent to YouTube and Instagram is extended.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# GTA 6 things worth a tag when they are said or written in the clip
GLOSSARY = [
    "vice city", "leonida", "leonida keys", "lucia", "jason", "wanted level", "six stars", "five stars", "cops", "police chase",
    "helicopter", "jet ski", "gator", "alligator", "florida man", "ocean beach", "vice beach", "grassrivers", "port gellhorn",
    "spike strips", "busted", "glitch", "ragdoll", "backflip", "flatbed", "easter egg", "ufo", "alien", "bigfoot", "skunk ape",
    "strip club", "lap dance", "tank", "train", "plane", "boat", "shark", "whale", "tiger", "crocodile", "croc", "scooter",
    "heist", "robbery", "car chase", "crash", "fail", "cheat code", "map", "trailer", "online", "mission",
]
SKIP_WORDS = set("""a an the and or but of to in on at for with from by as is are was were be been this that these those it its his her their
him them he she they we you i me my our your just very really also then than so too not no yes up down out over under after before again never
always still even more most some any all one two three four five six seven eight nine ten first last next new old big small little good bad
great best worst gets get got getting goes going went go makes make made making takes take took taking sees see saw seeing finds find found
finding stops stop stopped tries try tried trying loses lose lost losing says say said saying calls call called calling spots spot spotted
begs beg dies die died dying screams scream freaks freak rages rage loses runs run ran running turns turn turned looks look looked
hits hit survives survive blames blame eats eat throws throw drops drop steals steal blows blow crashes crash meets meet needs need
seconds second minutes minute times time way thing things guy guys man girl over into onto near around across through about while empty fast full
when where what who how why gta vi game stream clip""".split())
KEEP_ALWAYS = {"gta", "6", "vi"}
NOT_NAMES = {"The", "This", "That", "These", "Those", "Ew", "Oh", "Wow", "Yes", "No", "Just", "When", "What", "Why", "How", "And", "But",
             "His", "Her", "Their", "Now", "Then", "Here", "There", "Watch", "Full", "Follow", "Clip", "Stream", "Game", "Wait", "Man", "Bro"}
GENERIC_PROPER = {"Gta", "Vi", "Gtavi", "Reels", "Shorts"}


def _streamer_words(streamer: str) -> set:
    names = {streamer, streamer.replace(" Live", ""), "IShowSpeed", "Speed", "Kai", "Cenat", "LazarBeam", "Lazar", "Typical", "Gamer",
             "N3ON", "Mizo", "CarryMinati", "Carry"}
    try:
        for c in json.loads((HERE / "creators.json").read_text(encoding="utf-8")).get("creators", []):
            names.add(c.get("name", ""))
    except Exception:  # noqa: BLE001
        pass
    out = set()
    for n in names:
        out.add(n)
        out.update(n.split())
    return {x for x in out if x}


def _trending() -> list:
    try:
        return list(json.loads((HERE / "keywords_trending.json").read_text(encoding="utf-8")).get("terms", {}).keys())
    except Exception:  # noqa: BLE001
        return []


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"#\w+", " ", (text or "").replace("’", "'")))


def _words_text(d: dict) -> str:
    try:
        return " ".join(w.get("word", "") for w in (d.get("words") or []))
    except Exception:  # noqa: BLE001
        return ""


def _camel(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.findall(r"[A-Za-z0-9]+", s))


def topics(d: dict, streamer: str) -> list:
    """Ordered topic phrases from inside the clip, strongest first (names, GTA 6 things, objects)."""
    title, desc, hook = _clean(d.get("title") or d.get("tweet") or ""), _clean(d.get("desc") or ""), _clean(d.get("hook") or "")
    said = _clean(_words_text(d)).lower()
    own = _streamer_words(streamer)
    found, seen = [], set()

    def add(p):
        k = p.lower().strip()
        if k and k not in seen and len(k) > 2:
            seen.add(k)
            found.append(p.strip())

    # 1) names and proper nouns written in the title or description ("Dexter Morgan", "Satan", "UFO")
    for txt in (desc, title):
        for sent in re.split(r"(?<=[.!?])\s+", txt):
            toks = sent.split()
            for m in re.finditer(r"\b([A-Z][a-z]{2,}(?:'s)?(?:\s+[A-Z][a-z]{2,}(?:'s)?)?|[A-Z]{2,5})\b", sent):
                p = re.sub(r"'s\b", "", m.group(1))
                first = sent.lstrip().startswith(m.group(1))
                if first and txt is desc and p in NOT_NAMES:
                    continue
                if any(w in own or w in NOT_NAMES or w in GENERIC_PROPER for w in p.split()) or p.upper() in {"GTA", "VI", "AI"}:
                    continue
                if first and len(toks) > 1 and txt is desc and " " not in p:
                    continue  # sentence starter, not a name
                add(p)
    # 2) GTA 6 things said or written in the clip
    blob = f"{title} {desc} {hook} {said}".lower()
    for term in GLOSSARY + _trending():
        if re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])", blob) and term.lower() not in ("map", "online", "mission", "fail", "crash"):
            add(term)
    for term in ("fail", "crash", "map", "mission"):
        if re.search(r"(?<![a-z])" + term + r"(es|s|ed)?(?![a-z])", f"{title} {hook}".lower()):
            add(term)
    # 3) the main objects of the title: drop the streamer and the first verb, keep the nouns
    toks = [t for t in re.findall(r"[A-Za-z0-9']+", title) if t not in own]
    run = []

    def flush():
        if run:
            add(" ".join(run))
        run.clear()
    for t in toks[1:]:
        tl = re.sub(r"'s$", "", t.lower())
        if tl in SKIP_WORDS or len(tl) < 4 or tl.isdigit() or any(tl in x.lower().split() for x in found):
            flush()
            continue
        run.append(tl)
        if len(run) == 3:
            flush()
    flush()
    return found


def tags_for(d: dict, streamer: str, n: int = 4) -> list:
    """Hashtag-safe topic tags (CamelCase, no spaces), at most n, no duplicates, no generic filler."""
    out, seen = [], set()
    for p in topics(d, streamer):
        t = _camel(p)
        if not t or t.lower() in seen or t.lower() in ("gta", "gta6", "gtavi", "vi") or len(t) > 22:
            continue
        seen.add(t.lower())
        out.append(t)
        # also the single word of a two-word name (Dexter Morgan -> Dexter), it is what people search
        w = p.split()
        if len(w) == 2 and w[0][:1].isupper() and p.lower() not in GLOSSARY and len(out) < n and w[0].lower() not in seen:
            seen.add(w[0].lower())
            out.append(w[0])
        if len(out) >= n:
            break
    return out[:n]


def gta_title(title: str, limit: int = 88) -> str:
    """Post title always carries GTA 6. The on-video title is untouched."""
    t = (title or "").strip()
    if re.search(r"\bGTA\s*(6|VI)\b", t, re.I):
        return t[:limit]
    if re.search(r"\bGTA\b", t, re.I):  # "…in GTA" -> "…in GTA 6"
        return re.sub(r"\bGTA\b(?!\s*(6|VI))", "GTA 6", t, count=1, flags=re.I)[:limit]
    add = " in GTA 6"
    return (t[: limit - len(add)].rstrip() + add)


def _strip_tags(s: str) -> str:
    return re.sub(r"\s*#\w+", "", s or "").strip()


def youtube(d: dict, streamer_name: str, streamer_key: str, info: str = "") -> dict:
    tg = tags_for(d, streamer_key, 4)
    st = _camel(streamer_name)
    title = f"{gta_title(d.get('title') or '', 88)} #shorts"[:100]
    hashtags = ["#GTA6", "#GTAVI", f"#{st}"] + [f"#{t}" for t in tg] + ["#shorts"]
    desc = (f"{gta_title(d.get('title') or '', 120)}\n\n{_strip_tags(d.get('desc') or '')}\n\n"
            f"Clip from {streamer_name}'s stream. {info}\nAll credit to {streamer_name}. Watch the full stream on their channel.\n\n"
            + " ".join(hashtags))[:4900]
    tags, total = [], 0
    for t in ["GTA 6", "GTA VI", "GTA 6 gameplay", streamer_name] + [re.sub(r"(?<=[a-z])(?=[A-Z])", " ", x) for x in tg] + ["shorts", "clips"]:
        if t.lower() not in [x.lower() for x in tags] and total + len(t) < 450:
            tags.append(t)
            total += len(t) + 1
    return {"title": title, "description": desc, "tags": tags}


def instagram(d: dict, who: str, title: str = "") -> str:
    tg = tags_for(d, who, 4)
    st = _camel(who)
    hashtags = ["#GTA6", "#GTAVI", f"#{st}"] + [f"#{t}" for t in tg] + ["#gaming", "#reels"]
    parts = [gta_title(d.get("title") or title, 120), _strip_tags(d.get("desc") or "")]
    if d.get("src_url"):
        parts.append(f"\U0001F4FA Full video: {d['src_url']}")
    parts.append(f"All credit to {who}.")
    parts.append("Follow for daily GTA 6 streamer clips \U0001F514")
    parts.append(" ".join(hashtags))
    return "\n\n".join(x for x in parts if x)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        dd = json.loads(Path(p).read_text(encoding="utf-8"))
        print(json.dumps(youtube(dd, "IShowSpeed", "IShowSpeed"), indent=1, ensure_ascii=False))
        print(instagram(dd, "IShowSpeed"))
