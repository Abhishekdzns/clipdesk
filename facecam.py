"""Per-clip facecam finder. Looks at frames inside the clip only (the facecam moves and resizes during a stream),
finds the streamer's face with MediaPipe, then the facecam rectangle around it from its straight borders.
Returns a box 'x:y:w:h' in source pixels: the whole facecam (no gameplay, no black), or a crop around the face."""
import numpy as np


def _smooth(b, k=7):
    return np.convolve(b.astype(np.float32), np.ones(k) / k, mode="same")

def frame_box(G, th=14.0):
    """One frame (gray HxW). Picture-in-picture touching the left or right edge: a horizontal border from the
    edge to x=L, and a vertical border at x=L. Returns (x, y, w, h) in pixels or None."""
    H, W = G.shape
    gy = np.abs(np.diff(G, axis=0)) > th
    gx = np.abs(np.diff(G, axis=1)) > th
    gy = gy[:-1] | gy[1:]
    gx = gx[:, :-1] | gx[:, 1:]
    best = None
    for flip in (False, True):
        Y = gy[:, ::-1] if flip else gy
        X = gx[:, ::-1] if flip else gx
        runs = []
        for y in range(4, Y.shape[0] - 4):
            s = (_smooth(Y[y, :int(W * .75)]) > .5)[4:]
            L = (int(np.argmin(s)) if not s.all() else len(s)) + 4
            runs.append(L)
        runs = np.array([0] * 4 + runs + [0] * 4)
        rows = [y for y in range(len(runs)) if runs[y] >= W * .12]
        for yb in rows:
            Lb = runs[yb]
            tops = [-1] + [y for y in rows if y < yb and abs(runs[y] - Lb) <= 6]
            for yt in tops:
                h = yb - yt
                if h < H * .12 or h > H * .85:
                    continue
                ok = None
                for xr in range(max(1, Lb - 5), min(X.shape[1] - 1, Lb + 5)):
                    seg = X[max(0, yt + 3):yb - 2, xr]
                    if len(seg) and seg.mean() > .5:
                        ok = xr
                        break
                if ok is None:
                    continue
                w = ok + 1
                ar = w / h
                if not (1.0 <= ar <= 2.4):
                    continue
                area = w * h
                if best is None or area > best[0]:
                    best = (area, (W - w) if flip else 0, yt + 1, w, h)
    return None if best is None else best[1:]

def detect(frames):
    boxes = [b for b in (frame_box(f) for f in frames) if b]
    if len(boxes) < max(2, len(frames) // 5):
        return None, len(boxes)
    B = np.array(boxes, np.float32)
    # biggest cluster of similar boxes
    best, bi = -1, 0
    for i, b in enumerate(B):
        c = (np.abs(B - b).max(1) <= 10).sum()
        if c > best:
            best, bi = c, i
    m = np.median(B[np.abs(B - B[bi]).max(1) <= 10], 0)
    return tuple(int(round(v)) for v in m), len(boxes)


def frame_box_v(G, th=14.0):
    """Vertical-border method: the facecam's inner side is a long straight vertical edge."""
    H, W = G.shape
    gx = np.abs(np.diff(G, axis=1)) > th
    gx = gx[:, :-1] | gx[:, 1:]
    out = []
    for flip in (False, True):
        X = gx[:, ::-1] if flip else gx
        for x in range(int(W * .12), int(W * .6)):
            s = _smooth(X[:, x], 9) > .6
            if not s.any():
                continue
            d = np.diff(np.r_[0, s.astype(np.int8), 0])
            st, en = np.where(d == 1)[0], np.where(d == -1)[0]
            k = int(np.argmax(en - st))
            h = en[k] - st[k]
            if h < H * .15:
                continue
            w = x + 1
            if 1.0 <= w / h <= 2.4:
                out.append(((W - w) if flip else 0, int(st[k]), w, int(h)))
    return out




def _faces(src, times, sw, sh):
    import av_signals
    from mediapipe.tasks.python import vision
    from mediapipe.tasks.python.core.base_options import BaseOptions
    tiles = sorted({sh // 2, sh // 3, sh // 5}, reverse=True)
    opts = vision.FaceDetectorOptions(base_options=BaseOptions(model_asset_path=str(av_signals.model(
        "blaze_face_short_range.tflite"))), min_detection_confidence=0.55)
    imgs, hits = [], []
    with vision.FaceDetector.create_from_options(opts) as det:
        for k, t in enumerate(times):
            img = av_signals._grab(src, t, sw, sh)
            imgs.append(img)
            if img is not None:
                hits += [(k, *h) for h in av_signals._scan(img, det, tiles)]
    return imgs, hits


def _refine(grays, face, rect, slot_ar, sw, sh, log=print):
    """Frame the facecam nicely: the whole face with headroom and a bit of chest, never cutting the chin, never showing
    the overlay strips below the webcam. face = (x, y, w, h) of the face, rect = the border box or None (960-wide space).
    The bottom of the webcam is found as the strongest horizontal edge just under the chin (the rectangle finder often
    picks a wrong, too high bottom edge on dark game scenes)."""
    fx, fy, fw, fh = face
    cx, chin = fx + fw / 2, fy + fh
    if rect is not None:
        rx0, ry0, rw, rh = rect
        rx1, top_lim, rect_bottom = rx0 + rw, ry0, ry0 + rh
    else:
        rx0, rx1 = max(0.0, cx - 3.2 * fw), min(float(sw), cx + 3.2 * fw)
        top_lim, rect_bottom = max(0.0, fy - 1.3 * fh), None
    y_a, y_b = int(chin + 0.15 * fh), int(min(sh - 2, chin + 1.4 * fh))
    bottom = None
    if y_b - y_a > 6 and grays:
        xa, xb = int(rx0 + 0.1 * (rx1 - rx0)), int(rx1 - 0.1 * (rx1 - rx0))
        prof = np.zeros(y_b - y_a, np.float32)
        for g in grays:
            prof += np.abs(np.diff(g[y_a:y_b + 1, xa:xb], axis=0)).mean(1)
        prof /= len(grays)
        k = int(np.argmax(prof))
        if prof[k] > max(8.0, 2.5 * float(np.median(prof))):
            bottom = y_a + k + 1
    if bottom is None:
        bottom = rect_bottom if (rect_bottom is not None and rect_bottom >= chin + 0.3 * fh) else chin + 0.6 * fh
    bottom = min(float(sh), bottom)
    avail = max(10.0, bottom - top_lim)
    bh = min((rx1 - rx0) / slot_ar, avail)
    bw = bh * slot_ar
    y0 = bottom - bh
    if y0 > fy - 0.35 * fh:  # keep the hair and some headroom
        y0 = fy - 0.35 * fh
    y0 = max(top_lim, y0, 0.0)
    x0 = min(max(cx - bw / 2, rx0), rx1 - bw)
    return [x0, y0, bw, bh]


def find(src, s, e, W, H, slot_ar=1.745, n=14, log=print, side="left"):
    sw, sh = 960, int(round(H * 960 / W / 2) * 2)
    times = list(np.linspace(s + 1, max(s + 1.5, e - 1), n))
    imgs, hits = _faces(src, times, sw, sh)
    face = None
    # a face dead centre is a game character or a poster, webcams sit near the frame edges
    hits = [h for h in hits if not (.3 * sw < h[1] + h[3] / 2 < .7 * sw and .25 * sh < h[2] + h[4] / 2 < .75 * sh)]
    if side == "left":
        hits = [h for h in hits if h[1] + h[3] / 2 < sw * .45]
    elif side == "right":
        hits = [h for h in hits if h[1] + h[3] / 2 > sw * .55]
    if hits:
        h_ = np.array(hits, float)
        cx, cy = h_[:, 1] + h_[:, 3] / 2, h_[:, 2] + h_[:, 4] / 2
        bk, bn = None, 0
        for i in range(len(h_)):
            tol = max(h_[i, 3] * .8, 20)
            near = (abs(cx - cx[i]) < tol) & (abs(cy - cy[i]) < tol)
            c = len(set(h_[near, 0].astype(int)))
            if c > bn:
                bk, bn = near, c
        if bn >= 3:
            face = [float(np.median(h_[bk, j])) for j in (1, 2, 3, 4)]
    grays = [img.astype(np.float32).mean(2) for img in imgs if img is not None]
    cands = []
    for k, g in enumerate(grays):
        fb = frame_box(g)
        for b in frame_box_v(g) + ([fb] if fb else []):
            if side == "left" and b[0] > 4:
                continue
            if side == "right" and b[0] + b[2] < sw - 4:
                continue
            cands.append((k, b))
    if face is not None:  # the face must be on the facecam side too
        fx = face[0] + face[2] / 2
        if (side == "left" and fx > sw * .5) or (side == "right" and fx < sw * .5):
            face = None
    box = None
    if cands:
        def inside(b):
            if face is None:
                return True
            fx, fy = face[0] + face[2] / 2, face[1] + face[3] / 2
            return b[0] <= fx <= b[0] + b[2] and b[1] <= fy <= b[1] + b[3] and face[2] < b[2] * .8
        ok = [(k, b) for k, b in cands if inside(b)]
        best, bb = 0, None
        for _, b in ok:
            hit = {k for k, o in ok if max(abs(o[i] - b[i]) for i in range(4)) <= 12}
            if len(hit) > best:
                best, bb = len(hit), b
        if bb is not None and best >= 2:
            sel = np.array([o for _, o in ok if max(abs(o[i] - bb[i]) for i in range(4)) <= 12], np.float32)
            box = [float(v) for v in np.median(sel, 0)]
            box = [box[0] + 3, box[1] + 3, box[2] - 6, box[3] - 6]
            log(f"    facecam: rectangle {[int(v) for v in box]} seen in {best} frames")
            if face is not None:
                nb = _refine(grays, face, box, slot_ar, sw, sh)
                log(f"    facecam: framed with face and chest {[int(v) for v in nb]} (border box was {[int(v) for v in box]})")
                box = nb
    if box is None and face is not None:
        box = _refine(grays, face, None, slot_ar, sw, sh)
        log(f"    facecam: no clean border, framing around the face and chest {[int(v) for v in box]}")
    if box is None:
        log("    facecam: not found")
        return ""
    f = W / sw
    x, y, w, h = (v * f for v in box)
    return ":".join(str(int(v) // 2 * 2) for v in (x, y, w, h))


def verify(src, s, e, W, H, box, n=8):
    """Share of sampled frames inside the clip that have a face whose centre sits in box (x, y, w, h source pixels)."""
    sw, sh = 960, int(round(H * 960 / W / 2) * 2)
    times = list(np.linspace(s + 1, max(s + 1.5, e - 1), n))
    imgs, hits = _faces(src, times, sw, sh)
    k_ = sw / float(W)
    x, y, w, h = [v * k_ for v in box]
    ok = {hh[0] for hh in hits if x <= hh[1] + hh[3] / 2 <= x + w and y <= hh[2] + hh[4] / 2 <= y + h}
    got = sum(1 for im in imgs if im is not None)
    return len(ok) / float(got or 1)
