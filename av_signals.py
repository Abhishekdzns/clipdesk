"""Free, local signals that run on this PC (no Claude, no internet after the first model download):

  vocal_events  - YAMNet sound classes per second: laughing, screaming / shouting / yelling, gunfire.
  face_reaction - finds the facecam, then MediaPipe face tracking per second: jaw drop, raised brows,
                  wide eyes, sudden head moves, leaving the camera.

Both return per-second arrays plus tags ("LAUGHING", "SHOCKED FACE", ...) that the AI sees next to the words.
Results are cached next to the clips, so a second run on the same video is instant.
"""
from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
URLS = {
    "yamnet.tflite": "https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite",
    "blaze_face_short_range.tflite": "https://storage.googleapis.com/mediapipe-models/face_detector/"
                                     "blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
                            "float16/1/face_landmarker.task",
}
LAUGH = {"Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"}
SCREAM = {"Screaming", "Shout", "Bellow", "Whoop", "Yell", "Battle cry", "Children shouting"}
GUN = {"Gunshot, gunfire", "Machine gun", "Explosion", "Artillery fire", "Cap gun"}


def model(name: str) -> Path:
    p = MODELS / name
    if not p.exists() or p.stat().st_size < 1000:
        MODELS.mkdir(exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".part")
        urllib.request.urlretrieve(URLS[name], tmp)
        tmp.replace(p)
    return p


def available() -> tuple[bool, str]:
    try:
        import mediapipe  # noqa: F401
        from mediapipe.tasks.python import audio, vision  # noqa: F401
    except Exception as ex:  # noqa: BLE001
        return False, f"mediapipe not installed ({str(ex)[:80]}); run setup_vision.bat"
    return True, ""


def _ffmpeg_pipe(args: list):
    return subprocess.Popen(["ffmpeg", "-v", "error", *map(str, args)], stdout=subprocess.PIPE,
                            stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- sounds
def vocal_events(src: Path, dur: float, cache: Path, log=print) -> dict:
    if cache.exists():
        d = np.load(cache)
        return {k: d[k] for k in d.files}
    from mediapipe.tasks.python import audio as mpa
    from mediapipe.tasks.python.core.base_options import BaseOptions
    from mediapipe.tasks.python.components.containers import audio_data

    n = int(np.ceil(dur)) + 1
    out = {k: np.zeros(n, np.float32) for k in ("laugh", "scream", "gun")}
    opts = mpa.AudioClassifierOptions(base_options=BaseOptions(model_asset_path=str(model("yamnet.tflite"))),
                                      running_mode=mpa.RunningMode.AUDIO_CLIPS)
    sr, chunk = 16000, 60
    t0 = time.time()
    p = _ffmpeg_pipe(["-i", src, "-vn", "-ac", "1", "-ar", sr, "-f", "s16le", "-"])
    with mpa.AudioClassifier.create_from_options(opts) as clf:
        base = 0.0
        while True:
            raw = p.stdout.read(sr * chunk * 2)
            if not raw:
                break
            x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            for r in clf.classify(audio_data.AudioData.create_from_array(x, sr)):
                i = int(base + r.timestamp_ms / 1000.0)
                if not 0 <= i < n or not r.classifications:
                    continue
                for c in r.classifications[0].categories:
                    for key, names in (("laugh", LAUGH), ("scream", SCREAM), ("gun", GUN)):
                        if c.category_name in names and c.score > out[key][i]:
                            out[key][i] = c.score
            base += len(x) / sr
            if int(base) % 180 < chunk:
                log(f"      sounds: {int(base // 60)} / {int(dur // 60)} min")
    p.wait()
    log(f"    Sound events done in {time.time() - t0:.0f}s "
        f"(laughing {int((out['laugh'] > 0.3).sum())}s, screaming {int((out['scream'] > 0.3).sum())}s)")
    np.savez(cache, **out)
    return out


# ---------------------------------------------------------------- facecam
def _grab(src: Path, t: float, w: int, h: int):
    try:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", str(src), "-frames:v", "1",
                              "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, timeout=60).stdout
    except subprocess.TimeoutExpired:
        return None
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3) if len(raw) == w * h * 3 else None


def _scan(img, det, tiles):
    """Run the face detector on overlapping square tiles of several sizes (small facecams need small tiles,
    the short-range model only sees faces that fill a good part of its input)."""
    import mediapipe as mp
    H_, W_ = img.shape[:2]
    out = []
    for tile in tiles:
        tile = min(tile, H_, W_)
        step = max(1, tile // 2)
        ys = list(range(0, max(1, H_ - tile + 1), step))
        xs = list(range(0, max(1, W_ - tile + 1), step))
        if ys[-1] != H_ - tile:
            ys.append(max(0, H_ - tile))
        if xs[-1] != W_ - tile:
            xs.append(max(0, W_ - tile))
        for y0 in ys:
            for x0 in xs:
                crop = np.ascontiguousarray(img[y0:y0 + tile, x0:x0 + tile])
                for d in det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=crop)).detections:
                    b = d.bounding_box
                    if b.width < max(10, tile * 0.12):
                        continue
                    sc = d.categories[0].score if d.categories else 0.0
                    out.append((x0 + b.origin_x, y0 + b.origin_y, b.width, b.height, sc))
    return out


def find_face(src: Path, dur: float, W: int, H: int, log=print, frames: int = 24):
    """Look at 24 frames at up to 1080p, scan each in tiles of 3 sizes (down to 1/5 of the frame height, so
    small facecams are found), keep the spot where a face shows up in the most frames.
    Returns (x, y, w, h) of the face in source pixels, or None."""
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    sw = min(1920, int(W) // 2 * 2)
    sh_ = int(round(H * sw / W / 2) * 2)
    tiles = sorted({sh_ // 2, sh_ // 3, sh_ // 5}, reverse=True)
    opts = vision.FaceDetectorOptions(base_options=BaseOptions(model_asset_path=str(model(
        "blaze_face_short_range.tflite"))), min_detection_confidence=0.55)
    hits, got = [], 0
    t0 = time.time()
    with vision.FaceDetector.create_from_options(opts) as det:
        for k, frac in enumerate(np.linspace(0.04, 0.96, frames)):
            img = _grab(src, dur * frac, sw, sh_)
            if img is None:
                continue
            got += 1
            hits += [(k, *h) for h in _scan(img, det, tiles)]
    if not hits:
        log("    Facecam: no face found, skipping face reactions")
        return None
    hits = np.array(hits, float)  # k, x, y, w, h, score
    cx, cy = hits[:, 1] + hits[:, 3] / 2, hits[:, 2] + hits[:, 4] / 2
    # A webcam sits against an edge or corner. A face dead centre is usually a game character that shows up in many
    # frames and would beat the real webcam, so look at edge candidates first and only use centre ones if none exist.
    mid = (cx > 0.3 * sw) & (cx < 0.7 * sw) & (cy > 0.25 * sh_) & (cy < 0.75 * sh_)
    if (~mid).any():
        hits, cx, cy = hits[~mid], cx[~mid], cy[~mid]
    best, best_key = None, (0, 0.0)
    for i in range(len(hits)):
        tol = max(hits[i, 3] * 0.8, sw * 0.02)
        near = (abs(cx - cx[i]) < tol) & (abs(cy - cy[i]) < tol)
        k_ = (len(set(hits[near, 0].astype(int))), float(np.median(hits[near, 5])))
        if k_ > best_key:
            best, best_key = near, k_
    best_n = best_key[0]
    need = max(4, int(got * 0.3))
    if best_n < need:
        log(f"    Facecam: face only seen in {best_n} of {got} frames, skipping face reactions")
        return None
    f = W / sw
    x, y, w, h = (float(np.median(hits[best, j])) * f for j in (1, 2, 3, 4))
    log(f"    Facecam: face found in {best_n} of {got} frames at x={int(x)} y={int(y)} ({int(w)}px wide, "
        f"{time.time() - t0:.0f}s)")
    return x, y, w, h


def face_reaction(src: Path, dur: float, W: int, H: int, cache: Path, log=print, fps: int = 4) -> dict:
    if cache.exists():
        d = np.load(cache)
        return {k: d[k] for k in d.files}
    import mediapipe as mp
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions

    n = int(np.ceil(dur)) + 1
    keys = ("jaw", "brow", "eyes", "move", "present")
    out = {k: np.zeros(n, np.float32) for k in keys}
    face = find_face(src, dur, W, H, log)
    if face is None:
        out["present"][:] = -1  # marks "no facecam"
        np.savez(cache, **out)
        return out
    x, y, w, h = face
    cw, ch = min(W, w * 3.2), min(H, h * 3.0)
    cx, cy = max(0, min(W - cw, x + w / 2 - cw / 2)), max(0, min(H - ch, y + h / 2 - ch / 2))
    cw, ch, cx, cy = (int(v) // 2 * 2 for v in (cw, ch, cx, cy))
    ow = 256
    oh = int(round(ch * ow / cw / 2) * 2)
    opts = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model("face_landmarker.task"))),
        running_mode=vision.RunningMode.VIDEO, num_faces=1, output_face_blendshapes=True,
        min_face_detection_confidence=0.4, min_face_presence_confidence=0.4, min_tracking_confidence=0.4)
    p = _ffmpeg_pipe(["-hwaccel", "auto", "-i", src, "-an", "-vf",
                      f"fps={fps},crop={cw}:{ch}:{cx}:{cy},scale={ow}:{oh}", "-f", "rawvideo",
                      "-pix_fmt", "rgb24", "-"])
    fsize, k, prev, seen = ow * oh * 3, 0, None, np.zeros(n, np.float32)
    t0 = time.time()
    with vision.FaceLandmarker.create_from_options(opts) as lm:
        while True:
            raw = p.stdout.read(fsize)
            if len(raw) < fsize:
                break
            t = k / fps
            i = min(n - 1, int(t))
            seen[i] += 1
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.frombuffer(raw, np.uint8).reshape(oh, ow, 3))
            res = lm.detect_for_video(img, int(t * 1000))
            if res.face_landmarks:
                bs = {c.category_name: c.score for c in res.face_blendshapes[0]} if res.face_blendshapes else {}
                out["jaw"][i] = max(out["jaw"][i], bs.get("jawOpen", 0))
                out["brow"][i] = max(out["brow"][i], bs.get("browInnerUp", 0))
                out["eyes"][i] = max(out["eyes"][i], (bs.get("eyeWideLeft", 0) + bs.get("eyeWideRight", 0)) / 2)
                L = res.face_landmarks[0]
                nose, fw = np.array([L[1].x, L[1].y]), max(1e-3, abs(L[454].x - L[234].x))
                if prev is not None:
                    out["move"][i] = max(out["move"][i], float(np.linalg.norm(nose - prev) / fw))
                prev = nose
                out["present"][i] += 1
            else:
                prev = None
            k += 1
            if k % (fps * 180) == 0:
                log(f"      facecam: {int(t // 60)} / {int(dur // 60)} min")
    p.wait()
    out["present"] = np.where(seen > 0, out["present"] / np.maximum(seen, 1), out["present"])
    out["move"] = np.clip(out["move"], 0, 1.5)
    log(f"    Face reactions done in {time.time() - t0:.0f}s (face visible {int((out['present'] > 0.5).mean() * 100)}% "
        f"of the time)")
    np.savez(cache, **out)
    return out


# ---------------------------------------------------------------- turning it into signals and tags
def _box(x, k):
    return np.convolve(x, np.ones(k) / k, mode="same") if len(x) > k else x


def signals(ve: dict | None, fr: dict | None, n: int, rolling_z) -> dict:
    sig = {}
    if ve is not None:
        v = np.maximum(ve["laugh"][:n], ve["scream"][:n])
        sig["vocal"] = np.maximum(rolling_z(_box(v, 3), win=300), 0)
    if fr is not None and fr["present"].max() > 0:
        pres = fr["present"][:n]
        raw = 0.45 * fr["jaw"][:n] + 0.35 * fr["brow"][:n] + 0.2 * fr["eyes"][:n] + 0.8 * np.clip(fr["move"][:n], 0, 1)
        before = np.array([pres[max(0, i - 6):max(0, i - 1)].mean() if i > 1 else 0 for i in range(n)])
        left = (pres < 0.25) & (before > 0.8)
        raw = np.where(pres < 0.25, np.where(left, 0.6, 0.0), raw)
        sig["face"] = np.maximum(rolling_z(_box(raw, 2), win=300), 0)
    return sig


def tagger(ve: dict | None, fr: dict | None):
    """Returns tags_at(s, e) -> 'LAUGHING SCREAMING SHOCKED-FACE ...' for a stretch of seconds."""
    def tags_at(s: float, e: float) -> list:
        a, b = max(0, int(s)), max(int(s) + 1, int(np.ceil(e)))
        t = []
        if ve is not None:
            if ve["laugh"][a:b].max(initial=0) > 0.3:
                t.append("LAUGHING")
            if ve["scream"][a:b].max(initial=0) > 0.3:
                t.append("SCREAMING")
            if ve["gun"][a:b].max(initial=0) > 0.4:
                t.append("GUNFIRE")
        if fr is not None and fr["present"].max() > 0:
            jaw, brow, eyes = fr["jaw"][a:b], fr["brow"][a:b], fr["eyes"][a:b]
            if len(jaw) and ((jaw > 0.45) & ((brow > 0.35) | (eyes > 0.35))).any():
                t.append("SHOCKED FACE")
            elif len(jaw) and (jaw > 0.6).any():
                t.append("MOUTH WIDE OPEN")
            if fr["move"][a:b].max(initial=0) > 0.5:
                t.append("SUDDEN MOVE")
            pres = fr["present"]
            if a > 0 and (pres[a:b] < 0.25).any() and pres[max(0, a - 6):a].mean() > 0.8:
                t.append("LEFT CAMERA")
        return t
    return tags_at
