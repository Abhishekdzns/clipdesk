"""Instagram posting for Clipdesk (browser automation, no API).
  python ig_poster.py login                 one-time: opens a browser window, you sign in yourself, then close it
  python ig_poster.py post <clip.mp4>       post one clip as a Reel (caption taken from n3on_posts.json by file name)
  python ig_poster.py postnext              post the next clip from n3on_posts.json that is not in ig_posted.json
  python ig_poster.py postnext <name>       same, for any creator: reads <name>_posts.json (e.g. mrbeast -> mrbeast_posts.json)
Each entry in a posts file: file, title, description, and optional dir (clips folder, relative to this script)
Uses a saved browser profile in the ig_profile folder. Needs: pip install playwright ; playwright install chromium"""
import json, os, re, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
PROFILE = HERE / "ig_profile"
POSTED = HERE / "ig_posted.json"
POSTS = HERE / "n3on_posts.json"
CLIPS = HERE / "runs" / "live" / "N3ON_20261119_050015" / "clips"
SHOTS = HERE / "ig_shots"


def browser(p):
    PROFILE.mkdir(exist_ok=True)
    hl = os.environ.get("IG_HEADLESS") == "1"  # new headless mode: same browser as the visible one, so the saved sign-in keeps working
    return p.chromium.launch_persistent_context(
        str(PROFILE), headless=False, viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"] + (["--headless=new"] if hl else []))


def login():
    """Opens Instagram in a browser window. Sign in yourself (any account). The window closes by itself once you are in,
    and the account name is saved to ig_account.txt. With --fresh the old saved sign-in is moved aside first."""
    from playwright.sync_api import sync_playwright
    # Never moves the saved sign-in aside and never deletes ig_account.txt (a stray Switch account click once wiped both).
    # To change account: sign out inside this window first, then sign in with the other one.
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/")
        print("Sign in to Instagram in the window yourself. It closes by itself when you are in.", flush=True)
        t0 = time.time()
        while time.time() - t0 < 1200:
            try:
                if any(c.get("name") == "sessionid" for c in ctx.cookies("https://www.instagram.com")):
                    break
            except Exception:
                return
            page.wait_for_timeout(2500)
        else:
            print("Timed out waiting for sign-in."); ctx.close(); sys.exit(2)
        page.wait_for_timeout(5000)
        name = ""
        try:
            page.goto("https://www.instagram.com/")
            page.wait_for_timeout(4000)
            for sel in ['a[href$="/"][role="link"]:has-text("Profile")', 'a:has-text("Profile")']:
                try:
                    h = page.locator(sel).first.get_attribute("href", timeout=4000) or ""
                    m = re.match(r"^/([A-Za-z0-9._]+)/?$", h)
                    if m:
                        name = m.group(1); break
                except Exception:
                    continue
        except Exception:
            pass
        (HERE / "ig_account.txt").write_text(name or "signed in", encoding="utf-8")
        ctx.close()
    print("Saved. Account:", name or "signed in")


def mbs():
    """One-time: open Meta Business Suite in the Instagram profile. Click Continue with Instagram and approve it yourself.
    Closes by itself when Business Suite is open; writes ig_mbs.txt."""
    from playwright.sync_api import sync_playwright
    (HERE / "ig_mbs.txt").unlink(missing_ok=True)
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://business.facebook.com/latest/home")
        t0 = time.time()
        while time.time() - t0 < 1200:
            try:
                if "business.facebook.com/latest" in page.url and page.get_by_text("Planner", exact=True).count() > 0:
                    page.wait_for_timeout(6000)
                    (HERE / "ig_mbs.txt").write_text(page.url, encoding="utf-8"); break
            except Exception:
                return
            page.wait_for_timeout(2500)
        ctx.close()
    print("mbs done")


def check():
    """Probe Meta Business Suite: writes ig_shots/check.txt + check_*.png"""
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(exist_ok=True)
    out = []
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, u in enumerate(["https://business.facebook.com/latest/posts/scheduled_posts", "https://business.facebook.com/latest/content_calendar"]):
            try:
                page.goto(u); page.wait_for_timeout(12000)
                page.screenshot(path=str(SHOTS / f"check_{i}.png"))
                out.append("URL " + page.url + "\n" + page.inner_text("body")[:2500].replace("\n", " | "))
            except Exception as e:
                out.append("ERR " + u + " " + str(e)[:200])
        ctx.close()
    (SHOTS / "check.txt").write_text("\n\n".join(out), encoding="utf-8")
    print("check done")


def probe(mp4: Path):
    """Debug: open the Reel composer in Business Suite, add the clip, dump the page. Never publishes or schedules."""
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(exist_ok=True)
    out = []
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        def dump(tag):
            page.screenshot(path=str(SHOTS / f"probe_{tag}.png"), full_page=True)
            out.append("== " + tag + " " + page.url + "\n" + page.inner_text("body")[:3500].replace("\n", " | "))
            btn = page.evaluate("Array.from(document.querySelectorAll('button,[role=button],[role=switch],input,[role=radio],[role=combobox]')).map(e=>(e.getAttribute('role')||e.tagName)+':'+(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.type||'')+':'+(e.innerText||'').slice(0,30).replace(/\\n/g,' ')).slice(0,120)")
            out.append("ELEMS " + " ;; ".join(btn))
        try:
            page.goto("https://business.facebook.com/latest/reels_composer"); page.wait_for_timeout(10000); dump("1_open")
            with page.expect_file_chooser(timeout=20000) as fc:
                page.get_by_role("button", name=re.compile(r"^Add video$", re.I)).first.click()
            fc.value.set_files(str(mp4))
            page.wait_for_timeout(30000); dump("2_uploaded")
            for i in range(3):
                try:
                    page.get_by_role("button", name=re.compile(r"^Next$", re.I)).last.click(timeout=5000)
                except Exception:
                    break
                page.wait_for_timeout(5000); dump(f"3_next{i}")
            for lab in ["Set date and time", "Schedule", "Scheduling options"]:
                try:
                    page.get_by_text(re.compile(lab, re.I)).first.click(timeout=3000); page.wait_for_timeout(3000); dump("4_" + lab.replace(" ", "_")); break
                except Exception:
                    continue
        except Exception as e:
            out.append("ERR " + str(e)[:300])
        (SHOTS / "probe.txt").write_text("\n\n".join(out), encoding="utf-8")
        ctx.close()
    print("probe done")


def prog(txt: str):
    try:
        (HERE / "ig_progress.txt").write_text(txt, encoding="utf-8")
    except Exception:
        pass


def avatar():
    """Saves the Instagram profile picture: avatars/acct_ig.jpg"""
    from playwright.sync_api import sync_playwright
    acc = (HERE / "ig_account.txt").read_text(encoding="utf-8").strip() if (HERE / "ig_account.txt").exists() else ""
    if not acc or acc == "signed in":
        print("no account name"); sys.exit(2)
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(f"https://www.instagram.com/{acc}/"); page.wait_for_timeout(7000)
            src = None
            for sel in ['header img[alt*="profile picture"]', "header img"]:
                try:
                    src = page.locator(sel).first.get_attribute("src", timeout=4000)
                    if src:
                        break
                except Exception:
                    continue
            if not src:
                src = page.locator('meta[property="og:image"]').first.get_attribute("content", timeout=3000)
            data = ctx.request.get(src).body()
            (HERE / "avatars").mkdir(exist_ok=True)
            (HERE / "avatars" / "acct_ig.jpg").write_bytes(data)
            print("avatar ok")
        finally:
            ctx.close()


def menu(frag):
    """Explores the Scheduled list: opens the ... menu of the row that has this text, saves screenshots and the menu text."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        out = []
        try:
            page.goto("https://business.facebook.com/latest/posts/scheduled_posts"); page.wait_for_timeout(10000)
            try:
                page.get_by_role("button", name=re.compile(r"^Done$")).first.click(timeout=3000)
            except Exception:
                pass
            row = page.locator('div[role="row"], tr').filter(has_text=frag).first
            out.append("row text: " + row.inner_text(timeout=8000)[:300].replace("\n", " | "))
            row.get_by_role("button", name=re.compile(r"\.\.\.|More|Open Drop-down", re.I)).first.click(timeout=8000)
            page.wait_for_timeout(9000)
            page.screenshot(path=str(HERE / "ig_shots" / "menu_2.png"))
            out.append("buttons: " + " | ".join(sorted({(b.get_attribute("aria-label") or b.inner_text() or "").strip()[:40] for b in page.get_by_role("button").all()})))
            out.append("menuitems: " + " | ".join(page.get_by_role("menuitem").all_inner_texts())[:300])
            out.append("menu: " + " | ".join(t.strip() for t in page.locator('[role="menu"], [role="menuitem"], [role="dialog"]').all_inner_texts())[:800])
        except Exception as e:
            out.append("ERR " + repr(e)[:300])
            page.screenshot(path=str(HERE / "ig_shots" / "menu_err.png"))
        finally:
            (HERE / "ig_shots" / "menu.txt").write_text("\n".join(out), encoding="utf-8")
            ctx.close()


def posted():
    return json.loads(POSTED.read_text(encoding="utf-8")) if POSTED.exists() else []


def clickable(page, text, timeout=15000):
    page.get_by_role("button", name=re.compile(rf"^{text}$", re.I)).or_(
        page.get_by_text(re.compile(rf"^{text}$", re.I))).first.click(timeout=timeout)


def post(mp4: Path, caption: str):
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.instagram.com/")
            page.wait_for_timeout(4000)
            out_ = "login" in page.url or page.locator('input[name="username"]').count() > 0 \
                or page.get_by_role("button", name=re.compile(r"^Log in$", re.I)).count() > 0
            if out_:
                page.screenshot(path=str(SHOTS / (mp4.stem + "_signedout.png")))
                print("NOT LOGGED IN: press Sign in to Instagram in Clipdesk (Publishing)"); sys.exit(3)
            opened = False
            for sel in ['svg[aria-label="New post"]', 'svg[aria-label="Create"]', '[aria-label="New post"]', '[aria-label="Create"]', 'a:has-text("Create")', 'div[role="button"]:has-text("Create")']:
                try:
                    page.locator(sel).first.click(timeout=4000); opened = True; break
                except Exception:
                    continue
            if not opened:
                page.mouse.click(36, 627)
            page.wait_for_timeout(1500)
            try:
                page.get_by_text(re.compile(r"^Post$")).first.click(timeout=3000)
            except Exception:
                pass
            with page.expect_file_chooser(timeout=20000) as fc:
                page.get_by_text(re.compile(r"Select from computer", re.I)).first.click()
            fc.value.set_files(str(mp4))
            page.wait_for_timeout(4000)
            try:
                page.get_by_role("button", name=re.compile(r"^OK$", re.I)).first.click(timeout=4000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            page.screenshot(path=str(SHOTS / "crop_step_a.png"))
            try:
                try:
                    page.locator('svg[aria-label="Select crop"]').first.click(timeout=3000)
                except Exception:
                    page.mouse.click(324, 778)
                page.wait_for_timeout(1000)
                page.screenshot(path=str(SHOTS / "crop_step_b_menu.png"))
                chosen = False
                for label in ["Original", "9:16"]:
                    try:
                        page.get_by_text(label, exact=True).first.click(timeout=3000); chosen = True; break
                    except Exception:
                        continue
                print("crop_choice", chosen)
                page.wait_for_timeout(1000)
                page.screenshot(path=str(SHOTS / "crop_step_c_after.png"))
            except Exception as e:
                print("crop_menu_error", str(e)[:150])
            clickable(page, "Next", 60000)
            page.wait_for_timeout(1500)
            clickable(page, "Next", 30000)
            page.wait_for_timeout(1500)
            box = page.locator('div[role="textbox"][contenteditable="true"]').first
            box.click(timeout=20000)
            page.keyboard.insert_text(caption[:2150])
            page.wait_for_timeout(1000)
            page.screenshot(path=str(SHOTS / (mp4.stem + "_before_share.png")))
            if "--dry" in sys.argv:
                print(json.dumps({"dry_run_ready_to_share": mp4.name})); ctx.close(); return
            dlg = page.locator('div[role="dialog"]').last
            try:
                dlg.get_by_role("button", name=re.compile(r"^Share$", re.I)).first.click(timeout=8000)
            except Exception:
                dlg.get_by_text("Share", exact=True).last.click(timeout=12000)
            page.get_by_text(re.compile(r"has been shared", re.I)).first.wait_for(timeout=600000)
            print(json.dumps({"posted": mp4.name}))
            done = posted(); done.append(mp4.name)
            POSTED.write_text(json.dumps(done, indent=1), encoding="utf-8")
        except SystemExit:
            raise
        except Exception as e:
            page.screenshot(path=str(SHOTS / (mp4.stem + "_error.png")))
            print(json.dumps({"error": str(e)[:300]}), flush=True); os._exit(1)
        finally:
            ctx.close()


def schedule(mp4: Path, caption: str, at: int, dry: bool = False):
    """Schedule one Reel through Meta Business Suite (works while the PC is off afterwards). at = unix time, local clock."""
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            prog("Opening Business Suite")
            page.goto("https://business.facebook.com/latest/reels_composer"); page.wait_for_timeout(9000)
            if "loginpage" in page.url or page.get_by_text("Continue with Instagram").count() > 0:
                page.screenshot(path=str(SHOTS / (mp4.stem + "_mbs_signedout.png")))
                print("NOT LOGGED IN: press Connect Meta Business Suite in Clipdesk (Publishing)"); sys.exit(3)
            with page.expect_file_chooser(timeout=20000) as fc:
                page.get_by_role("button", name=re.compile(r"^Add video$", re.I)).first.click()
            prog("Sending video to Instagram")
            fc.value.set_files(str(mp4))
            page.get_by_text(re.compile(r"No copyright issues|safe to publish", re.I)).first.wait_for(timeout=420000)
            prog("Meta checked the video")
            box = page.locator('div[role="textbox"][contenteditable="true"]').first
            box.click(timeout=20000)
            page.keyboard.insert_text(caption[:2150])
            prog("Caption added")
            page.wait_for_timeout(1000)
            for _ in range(2):
                page.get_by_role("button", name=re.compile(r"^Next$", re.I)).last.click(timeout=30000)
                page.wait_for_timeout(5000)
            page.get_by_role("button", name=re.compile(r"^Schedule$", re.I)).first.click(timeout=15000)
            page.wait_for_timeout(2500)
            prog("Setting the go-live time")
            d = time.localtime(at)
            di = page.locator('input[placeholder="dd/mm/yyyy"], input[aria-label="dd/mm/yyyy"]').first
            di.click(); page.keyboard.press("Control+A"); page.keyboard.type("%02d/%02d/%04d" % (d.tm_mday, d.tm_mon, d.tm_year)); page.keyboard.press("Tab")
            for nm, val in (("hours", d.tm_hour), ("minutes", d.tm_min)):
                sp = page.get_by_role("spinbutton", name=re.compile(nm, re.I)).first
                sp.click(); page.keyboard.press("Control+A"); page.keyboard.type("%02d" % val); page.keyboard.press("Tab")
            page.wait_for_timeout(1500)
            body = page.inner_text("body")
            want_d = "%d %s %d" % (d.tm_mday, time.strftime("%B", d), d.tm_year)
            ok_d = want_d in di.input_value()
            ok_t = re.search(r"%02d\s*:\s*%02d" % (d.tm_hour, d.tm_min), body) is not None
            page.screenshot(path=str(SHOTS / (mp4.stem + "_before_schedule.png")))
            print(json.dumps({"date_ok": ok_d, "time_ok": ok_t, "wanted": want_d + time.strftime(" %H:%M", d)}), flush=True)
            if dry or not (ok_d and ok_t):
                print(json.dumps({"dry_run_ready_to_schedule": mp4.name} if dry else {"error": "date or time not set as wanted"})); sys.stdout.flush(); os._exit(0 if dry else 1)
            prog("Scheduling")
            page.get_by_role("button", name=re.compile(r"^Schedule$", re.I)).last.click(timeout=15000)
            page.wait_for_timeout(10000)
            page.screenshot(path=str(SHOTS / (mp4.stem + "_after_schedule.png")))
            done = page.get_by_role("button", name=re.compile(r"^Schedule$", re.I)).count() == 0 or page.get_by_text(re.compile(r"scheduled", re.I)).count() > 0
            if not done:
                raise RuntimeError("Schedule button still there after click")
            print(json.dumps({"scheduled": mp4.name, "at": at}))
            done_l = posted(); done_l.append(mp4.name)
            POSTED.write_text(json.dumps(done_l, indent=1), encoding="utf-8")
        except SystemExit:
            raise
        except Exception as e:
            page.screenshot(path=str(SHOTS / (mp4.stem + "_error.png")))
            print(json.dumps({"error": str(e)[:300]}), flush=True); os._exit(1)
        finally:
            ctx.close()


def all_posts():
    out = []
    for f in sorted(HERE.glob("*_posts.json")):
        try:
            out += json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return out


def clip_path(r) -> Path:
    return (HERE / r["dir"] / r["file"]) if r.get("dir") else CLIPS / r["file"]


def caption_for(name: str, mp4=None) -> str:
    for r in all_posts():
        if r["file"] == name:
            d = r["description"].replace(" #shorts", "")
            return f"{r['title']}\n\n{d}"
    if mp4 is not None:  # clip from Clipdesk with no posts file: build the caption from its edit.json
        try:
            side = Path(mp4).with_suffix(".edit.json")
            d = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
            who = re.sub(r"_\d{8}_\d{6}$", "", Path(mp4).parent.parent.name).replace("_", " ")
            title = (d.get("title") or Path(mp4).stem).strip()
            try:  # caption built from what is inside the clip (seo.py): GTA 6 in the first line, topic hashtags
                import seo
                return seo.instagram(d, who, title)
            except Exception:  # noqa: BLE001
                pass
            parts = [title, str(d.get("desc") or "").strip()]
            if d.get("src_url"):
                parts.append(f"\U0001F4FA Full video: {d['src_url']}")
            parts.append(f"All credit to {who}.")
            parts.append("Follow for daily GTA 6 streamer clips \U0001F514")
            parts.append(f"#GTA6 #GTAVI #gaming #{re.sub(r'[^A-Za-z0-9]', '', who)}")
            return "\n\n".join(x for x in parts if x)
        except Exception:
            pass
    return name


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["menu"] and len(a) > 1:
        menu(a[1])
    elif a[:1] == ["avatar"]:
        avatar()
    elif a[:1] == ["probe"] and len(a) > 1:
        probe(Path(a[1]))
    elif a[:1] == ["mbs"]:
        mbs()
    elif a[:1] == ["check"]:
        check()
    elif a[:1] == ["login"]:
        login()
    elif a[:1] == ["post"] and len(a) > 1 and "--at" in a:
        f = Path(a[1])
        if not f.exists():
            f = next((clip_path(r) for r in all_posts() if r["file"] == a[1]), CLIPS / a[1])
        schedule(f, caption_for(f.name, f), int(a[a.index("--at") + 1]), "--dry" in a)
    elif a[:1] == ["post"] and len(a) > 1:
        f = Path(a[1])
        if not f.exists():
            f = next((clip_path(r) for r in all_posts() if r["file"] == a[1]), CLIPS / a[1])
        post(f, caption_for(f.name, f))
    elif a[:1] == ["postnext"]:
        done = posted()
        pf = HERE / (f"{a[1]}_posts.json" if len(a) > 1 and not a[1].startswith("--") else "n3on_posts.json")
        for r in json.loads(pf.read_text(encoding="utf-8")):
            if r["file"] not in done:
                post(clip_path(r), caption_for(r["file"])); break
        else:
            print("all posted")
    else:
        print(__doc__)
