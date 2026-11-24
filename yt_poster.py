"""YouTube Shorts posting for Clipdesk.
  python yt_poster.py login              one-time: opens the browser, you sign in and pick the channel (token saved in yt_token.json)
  python yt_poster.py post <clip.mp4> <Streamer>   upload one clip, prints {"id": ..., "privacy": ...}
Needs client_secret.json (Google Cloud OAuth client, Desktop app) in this folder. Free YouTube Data API."""
import time
import json, re, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
SECRET, TOKEN = HERE / "client_secret.json", HERE / "yt_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
NAMES = {"Kai Cenat Live": "Kai Cenat", "Carryislive": "CarryMinati", "IShowSpeed": "IShowSpeed", "N3ON": "N3ON", "Mizo": "Mizo"}


def creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if c and c.expired and c.refresh_token:
        c.refresh(Request())
        TOKEN.write_text(c.to_json(), encoding="utf-8")
    return c if c and c.valid else None


def login():
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not SECRET.exists():
        print("client_secret.json is missing in the clipper folder"); sys.exit(2)
    c = InstalledAppFlow.from_client_secrets_file(str(SECRET), SCOPES).run_local_server(port=0, prompt="consent")
    TOKEN.write_text(c.to_json(), encoding="utf-8")
    from googleapiclient.discovery import build
    me = build("youtube", "v3", credentials=c).channels().list(part="snippet", mine=True).execute()
    name = (me.get("items") or [{}])[0].get("snippet", {}).get("title", "?")
    (HERE / "yt_channel.txt").write_text(name, encoding="utf-8")
    print("LOGGED IN as channel:", name)


def post(mp4: Path, streamer: str, at: int = 0):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    c = creds()
    if not c:
        print(json.dumps({"error": "not logged in: run yt_poster.py login"})); sys.exit(3)
    side = mp4.with_suffix(".edit.json")
    d = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    info_f = mp4.with_suffix(".info.txt")
    info = info_f.read_text(encoding="utf-8").splitlines()[1] if info_f.exists() and len(info_f.read_text(encoding="utf-8").splitlines()) > 1 else ""
    title = (d.get("title") or mp4.stem).strip()
    name = NAMES.get(streamer, streamer)
    tag = re.sub(r"[^A-Za-z0-9]", "", name)
    try:  # title + tags built from what is inside the clip (seo.py): GTA 6 in the title, topic tags like the names and objects shown
        import seo
        sd = seo.youtube(d, name, streamer, info)
        yt_title, desc, tags = sd["title"], sd["description"], sd["tags"]
    except Exception:  # noqa: BLE001
        yt_title = f"{title} in GTA 6 #shorts"[:100]
        desc = (f"{title}\n\nClip from {name}'s stream. {info}\n"
                f"All credit to {name}. Watch the full stream on their channel.\n\n#GTA6 #GTAVI #{tag} #shorts")[:4900]
        tags = ["GTA 6", "GTA VI", name, "shorts", "clips"]
    body = {"snippet": {"title": yt_title, "description": desc, "categoryId": "20", "tags": tags},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    if at:  # schedule: stays private until YouTube makes it public at this time
        import datetime as _dt
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = _dt.datetime.fromtimestamp(at, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    yt = build("youtube", "v3", credentials=c)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(mp4), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    pf = HERE / "yt_progress.txt"
    pf.write_text("0", encoding="utf-8")
    while resp is None:
        stt, resp = req.next_chunk()
        if stt:
            pf.write_text(str(int(stt.progress() * 100)), encoding="utf-8")
    pf.write_text("100", encoding="utf-8")
    print(json.dumps({"id": resp.get("id"), "privacy": resp.get("status", {}).get("privacyStatus")}))


def avatar():
    """Saves the channel picture and name: avatars/acct_yt.jpg, yt_channel.txt"""
    import urllib.request
    from googleapiclient.discovery import build
    c = creds()
    if not c:
        print("not logged in"); sys.exit(3)
    it = (build("youtube", "v3", credentials=c).channels().list(part="snippet", mine=True).execute().get("items") or [{}])[0].get("snippet", {})
    th = it.get("thumbnails", {})
    url = (th.get("medium") or th.get("default") or {}).get("url")
    if url:
        (HERE / "avatars").mkdir(exist_ok=True)
        (HERE / "avatars" / "acct_yt.jpg").write_bytes(urllib.request.urlopen(url, timeout=20).read())
    if it.get("title"):
        (HERE / "yt_channel.txt").write_text(it["title"], encoding="utf-8")
    print("avatar ok")


def resched(vid, at):
    """Moves a scheduled YouTube video to a new go-live time."""
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", credentials=creds())
    stt = yt.videos().list(part="status", id=vid).execute()["items"][0]["status"]
    stt["privacyStatus"] = "private"
    stt["publishAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(at))
    yt.videos().update(part="status", body={"id": vid, "status": stt}).execute()
    print("rescheduled", vid, at)


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "resched" and sys.argv[2] == "killstale":
        import subprocess
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ig_poster.py*' -and $_.Name -like 'python*' -and "
              "((Get-Date) - $_.CreationDate).TotalMinutes -gt 10 } | ForEach-Object { taskkill /PID $_.ProcessId /T /F; 'killed ' + $_.ProcessId }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=40)
        print("rescheduled killstale", (r.stdout or "").replace("\n", " ")[:300])
    elif len(sys.argv) > 3 and sys.argv[1] == "resched":
        resched(sys.argv[2], int(sys.argv[3]))
    elif len(sys.argv) > 1 and sys.argv[1] == "avatar":
        avatar()
    elif len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    elif len(sys.argv) > 3 and sys.argv[1] == "post":
        post(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 0)
    else:
        print(__doc__)
