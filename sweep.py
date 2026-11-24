"""After-stream sweep: once YouTube has processed a stream that live mode watched, read the whole video once to catch
moments the live pass missed. Runs from Clipdesk every 30 min. Queue: sweep_todo.json. Log: sweep_log.txt"""
import json, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TODO = HERE / "sweep_todo.json"


def say(x):
    with open(HERE / "sweep_log.txt", "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S ") + str(x) + "\n")


def main():
    if not TODO.exists():
        return
    import clipper
    lst = json.loads(TODO.read_text(encoding="utf-8"))
    left = []
    for job in lst:
        if job.get("done"):
            continue
        r = subprocess.run(["yt-dlp", *clipper.yt_extra(), "--skip-download", "--print", "%(live_status)s", job["url"]],
                           capture_output=True, text=True, errors="replace")
        status = (r.stdout.strip().splitlines() or ["?"])[-1]
        if status != "was_live":
            say(f"{job['name']}: still {status}, waiting for YouTube to process it")
            left.append(job)
            continue
        say(f"{job['name']}: processed, reading the whole video")
        p = subprocess.run([sys.executable, "-u", "run_streamer.py", job["name"], "--url", job["url"]], cwd=str(HERE))
        job["done"] = p.returncode == 0
        say(f"{job['name']}: sweep {'done' if job['done'] else 'failed, will retry'}")
        if not job["done"]:
            left.append(job)
    TODO.write_text(json.dumps(left, indent=1), encoding="utf-8")


main()
