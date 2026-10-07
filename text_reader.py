"""
text_reader.py - reads text from the screen for the GPO bot (and anything else later).

Uses RapidOCR (ONNX, runs on CPU, no extra install):  pip install rapidocr-onnxruntime
Reading a known one-line area (candy counter, spawn banner) takes ~30-60 ms.

    from text_reader import TextReader
    tr = TextReader(monitor=1)
    text, conf = tr.read_area([0.40, 0.78, 0.60, 0.82])   # area as fractions of the screen
    tr.candies([...])  -> (282, 500) or None
    tr.find_in_area([...]) -> [(text, conf), ...]       # text anywhere in a loose area (slower)

Game text is usually outlined/colored, so the reader:
  * trims the area to where the text actually is (high-contrast pixels),
  * scales it so letters are big enough for the model,
  * runs recognition only (no detection pass) for single lines - much more reliable on small text.
Fuzzy helpers fix small OCR slips ("Spavned" -> "spawned", "GOMU" -> "Gomu").
"""
import difflib
import re
import threading

import cv2
import mss
import numpy as np

_engine = None
_engine_lock = threading.Lock()
_run_lock = threading.Lock()   # the OCR engine is shared by the bot and the spawn watcher


def engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _engine = RapidOCR()
        return _engine


def _rec(imgs):
    """Recognizer call that works with old (text_recognizer) and new (text_rec) RapidOCR versions."""
    e = engine()
    fn = getattr(e, "text_rec", None) or getattr(e, "text_recognizer")
    with _run_lock:
        return fn(imgs)


def _det(img):
    e = engine()
    fn = getattr(e, "text_det", None) or getattr(e, "text_detector")
    with _run_lock:
        return fn(img)


def trim_to_text(img, margin=4):
    """Crop to the bounding box of high-contrast (text-like) pixels."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    edges = cv2.Canny(gray, 60, 160)
    pts = cv2.findNonZero(edges)
    if pts is None:
        return img
    x, y, w, h = cv2.boundingRect(pts)
    if w < 8 or h < 6:
        return img
    H, W = gray.shape
    return img[max(0, y - margin):min(H, y + h + margin), max(0, x - margin):min(W, x + w + margin)]


def scale_for_ocr(img, target_h=48, max_scale=4.0):
    h = img.shape[0]
    s = min(max_scale, max(1.0, target_h / max(1, h)))
    if s > 1.01:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    return img


def read_line(img):
    """One line of text from an image (BGR). Returns (text, confidence)."""
    if img is None or img.size == 0:
        return "", 0.0
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    img = scale_for_ocr(trim_to_text(img))
    text, conf = _rec_one(img)
    if conf < 0.85:                                  # unsure: also try bigger + sharpened, keep the best
        big = cv2.resize(img, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)
        sharp = cv2.addWeighted(big, 1.6, cv2.GaussianBlur(big, (0, 0), 2), -0.6, 0)
        t2, c2 = _rec_one(sharp)
        if c2 > conf:
            text, conf = t2, c2
    return text, conf


def _rec_one(img):
    out = _rec([img])
    res = out[0] if isinstance(out, tuple) else out
    if not res:
        return "", 0.0
    text, conf = res[0][0], float(res[0][1])
    return text.strip(), conf


def find_text_boxes(img, scale=1.0):
    """Every line of text in the image with its box: [(text, conf, (x0, y0, x1, y1)), ...]."""
    if img is None or img.size == 0:
        return []
    big = img if scale == 1.0 else cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    out = _det(big)
    boxes = out[0] if isinstance(out, tuple) else out
    if boxes is None or len(boxes) == 0:
        return []
    found = []
    for b in boxes:
        b = np.array(b).astype(int)
        (x0, y0), (x1, y1) = b.min(0), b.max(0)
        text, conf = read_line(big[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3])
        if text:
            found.append((text, conf, (int(x0 / scale), int(y0 / scale), int(x1 / scale), int(y1 / scale))))
    return found


def similar(a, b):
    a, b = re.sub(r"[^a-z0-9]", "", a.lower()), re.sub(r"[^a-z0-9]", "", b.lower())
    if not a or not b:
        return 0.0
    if b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def find_text(img, scale=2.0):
    """Text somewhere inside a loose area (e.g. a banner whose width changes): finds each line first,
    then reads it. ~0.5-1.5 s, so use it for things checked every few seconds, not every frame.
    Returns [(text, conf), ...] top to bottom."""
    if img is None or img.size == 0:
        return []
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    out = _det(big)
    boxes = out[0] if isinstance(out, tuple) else out
    if boxes is None or len(boxes) == 0:
        return []
    # some OCR versions split one line into pieces: merge boxes that sit on the same line
    rects = []
    for b in boxes:
        b = np.array(b).astype(int)
        (x0, y0), (x1, y1) = b.min(0), b.max(0)
        rects.append([x0, y0, x1, y1])
    rects.sort(key=lambda r: (r[1] + r[3]) / 2)
    lines = []
    for r in rects:
        cy, h = (r[1] + r[3]) / 2, r[3] - r[1]
        for L in lines:
            lcy, lh = (L[1] + L[3]) / 2, L[3] - L[1]
            gap = max(L[0], r[0]) - min(L[2], r[2])          # horizontal gap (negative = overlap)
            if abs(cy - lcy) < 0.5 * max(h, lh) and gap < 1.5 * max(h, lh):
                L[0], L[1], L[2], L[3] = min(L[0], r[0]), min(L[1], r[1]), max(L[2], r[2]), max(L[3], r[3])
                break
        else:
            lines.append(list(r))
    found = []
    for x0, y0, x1, y1 in lines:
        crop = big[max(0, y0 - 3):y1 + 3, max(0, x0 - 3):x1 + 3]
        text, conf = read_line(crop)
        if text:
            found.append((y0, text, conf))
    return [(t, c) for _, t, c in sorted(found)]


def fuzzy_pick(word, choices, cutoff=0.6):
    """Closest known name for an OCR'd word, or None."""
    if not word:
        return None
    lower = {c.lower(): c for c in choices}
    m = difflib.get_close_matches(word.lower(), list(lower), n=1, cutoff=cutoff)
    return lower[m[0]] if m else None


# ----------------------------------------------------------------------------- game vocabulary
RARITY = ["Common", "Rare", "Epic", "Legendary", "Mythical"]
FRUITS = {
    "Common": ["Suke", "Kilo", "Spin", "Heal"],
    "Rare": ["Bari", "Mero", "Horo", "Gomu", "Bomu"],
    "Epic": ["Yomi", "Spring", "Kira"],
    "Legendary": ["Mera", "Pika", "Hie", "Magu", "Goro", "Gura", "Zushi", "Suna", "Ito", "Paw", "Yuki",
                  "Kage", "Yami", "Goru", "Smoke", "Biscuit"],
    "Mythical": ["Tori", "Mochi", "Ope", "Venom", "Buddha", "Pteranodon", "Dragon", "Soul", "Leopard"],
}
FRUIT_RARITY = {f: r for r, fs in FRUITS.items() for f in fs}
FRUIT_ALIASES = {"bomb": "Bomu", "barrier": "Bari", "rubber": "Gomu", "flame": "Mera", "light": "Pika",
                 "ice": "Hie", "magma": "Magu", "sand": "Suna", "string": "Ito", "dark": "Yami"}
PLACES = ["Shell's Town", "Orange Town", "Baratie", "Arlong Park", "Coco Island", "Colosseum", "Sandora",
          "Roca Island", "Fishman Island", "Reverse Mountain", "Marine Base G-1", "Marine Fort F-1",
          "Gravito's Fort", "Kori Island", "Sky Island", "Upper Yard", "Logue Town", "Island Of Zou",
          "Las Camp", "Mysterious Island", "Fort F-1", "Desert Island"]


def fruit_lookup(word, cutoff=0.6):
    """OCR'd fruit name -> (canonical name, rarity), or (None, None). 'MERA'->Mera, 'Horc'->Horo, 'BOMB'->Bomu."""
    w = re.sub(r"[^a-z]", "", (word or "").lower())
    if len(w) < 2:
        return None, None
    if w in FRUIT_ALIASES:
        f = FRUIT_ALIASES[w]
        return f, FRUIT_RARITY[f]
    f = fuzzy_pick(w, list(FRUIT_RARITY), cutoff=cutoff)
    return (f, FRUIT_RARITY[f]) if f else (None, None)


def rarity_at_least(rarity, minimum):
    try:
        return RARITY.index(rarity) >= RARITY.index(minimum)
    except ValueError:
        return False


UNBOX_RE = re.compile(r"unbo\w*\s*(?:an?\s*)?([a-z]+)", re.I)


def parse_unbox(text):
    """'You unboxed a Horo!' (OCR: 'Yovunboxeda Horc!') -> ('Horo', 'Rare') or None."""
    t = re.sub(r"unbo\w*?ed\s*a\s*", "unboxed a ", text, flags=re.I)   # 'unboxeda' -> 'unboxed a'
    m = UNBOX_RE.search(t)
    if not m:
        return None
    f, r = fruit_lookup(m.group(1))
    return (f, r) if f else None


CANDY_RE = re.compile(r"(\d[\d,.\s]*)\s*[/|lI]\s*(\d[\d,.\s]*)")
SPAWN_RE = re.compile(r"^\s*(?:an?\s+)?(.+?)\s*ha[sz]\s*sp\w*?ed\s*at\s*(.+)$", re.I)


def parse_candies(text):
    m = CANDY_RE.search(text.replace("O", "0").replace("o", "0"))
    if not m:
        return None
    try:
        a = int(re.sub(r"\D", "", m.group(1)))
        b = int(re.sub(r"\D", "", m.group(2)))
    except ValueError:
        return None
    if b <= 0 or a > b * 2:
        return None
    return a, b


ITEM_RE = re.compile(r"ne[wv]\s*item\s*[:\-]?\s*[<(\[{]?\s*(.+?)\s*[>)\]}?z2]?\s*$", re.I)


def parse_item(text, known=None, strict=False):
    """'New Item <Nightfall Aura>' -> 'Nightfall Aura'. OCR slips ('Nev', 'Auraz') are fixed by matching
    against the known item names when given. Returns None if the line isn't a new-item message."""
    m = ITEM_RE.search(text.strip())
    if not m:
        return None
    raw = m.group(1).strip(" <>()[]{}.!?")
    if known:
        hit = fuzzy_pick(raw, known, cutoff=0.6)
        if hit:
            return hit
        for k in known:                             # e.g. 'Nightfall Auraz' -> 'Nightfall Aura'
            if similar(raw, k) >= 0.8:
                return k
        if strict:
            return None                             # e.g. 'New Item <Rare Fruit Chest>' from buying chests
    return raw


def parse_spawn(text, fruits=None, places=None):
    """'A GOMU has Spavned at SHELL'S TOWN' -> ('Gomu', "Shell's Town")."""
    m = SPAWN_RE.search(text)
    if not m:
        return None
    fruit, place = m.group(1).strip(" .!"), m.group(2).strip(" .!")
    # the message always starts with "A"; if OCR glued it to the name ("AGOMU"), drop it
    if not re.match(r"^\s*an?\s", text, re.I) and len(fruit) > 2 and fruit[0] in "Aa":
        fruit = fruit[1:]
    nice = lambda t: " ".join(w[:1].upper() + w[1:].lower() for w in t.split())   # SHELL'S -> Shell's
    if fruits:
        fruit = fuzzy_pick(fruit, fruits) or nice(fruit)
    else:
        fruit = fruit_lookup(fruit)[0] or nice(fruit)
    place = fuzzy_pick(place, places or PLACES, cutoff=0.7) or nice(place)
    return fruit, place


class TextReader:
    """Reads areas given as fractions of the game rectangle (the Roblox window, see screen.py)."""

    def __init__(self, monitor=1):
        self.sct = mss.mss()
        self._mon = None

    @property
    def mon(self):
        if self._mon is not None:            # fixed rect (tests / screenshots)
            return self._mon
        import screen
        return screen.capture_rect()

    @mon.setter
    def mon(self, value):
        self._mon = value

    def grab_area(self, area):
        x0, y0, x1, y1 = area
        m = self.mon
        W, H = m["width"], m["height"]
        box = {"left": m["left"] + int(x0 * W), "top": m["top"] + int(y0 * H),
               "width": max(1, int((x1 - x0) * W)), "height": max(1, int((y1 - y0) * H))}
        return cv2.cvtColor(np.array(self.sct.grab(box)), cv2.COLOR_BGRA2BGR)

    def read_area(self, area):
        """Tight area holding one line (candy counter). Fast."""
        return read_line(self.grab_area(area))

    def locate_text(self, target, area=(0, 0, 1, 1), min_sim=0.75, scale=1.0):
        """Find `target` text on screen. Returns (screen_x, screen_y, text) of its center, or None."""
        img = self.grab_area(area)
        best = None
        for text, conf, (x0, y0, x1, y1) in find_text_boxes(img, scale):
            sim = similar(text, target)
            # prefer the line that IS the target over a longer line that contains it
            sim -= 0.01 * abs(len(re.sub(r"\W", "", text)) - len(re.sub(r"\W", "", target)))
            if sim >= min_sim and (best is None or sim > best[0]):
                best = (sim, (x0 + x1) / 2, (y0 + y1) / 2, text)
        if not best:
            return None
        W, H = self.mon["width"], self.mon["height"]
        ax, ay = self.mon["left"] + area[0] * W, self.mon["top"] + area[1] * H
        return int(ax + best[1]), int(ay + best[2]), best[3]

    def locate_any(self, targets, area=(0, 0, 1, 1), min_sim=0.75, scale=1.0):
        """Like locate_text, but for several possible words at once (one screen read).
        Returns (screen_x, screen_y, text, target) or None."""
        img = self.grab_area(area)
        best = None
        for text, conf, (x0, y0, x1, y1) in find_text_boxes(img, scale):
            for target in targets:
                sim = similar(text, target)
                sim -= 0.01 * abs(len(re.sub(r"\W", "", text)) - len(re.sub(r"\W", "", target)))
                if sim >= min_sim and (best is None or sim > best[0]):
                    best = (sim, (x0 + x1) / 2, (y0 + y1) / 2, text, target)
        if not best:
            return None
        W, H = self.mon["width"], self.mon["height"]
        ax, ay = self.mon["left"] + area[0] * W, self.mon["top"] + area[1] * H
        return int(ax + best[1]), int(ay + best[2]), best[3], best[4]

    def locate_box(self, targets, area=(0, 0, 1, 1), min_sim=0.75, scale=1.0):
        """Like locate_any but returns the text's screen box too: (cx, cy, text, target, (x0, y0, x1, y1))."""
        img = self.grab_area(area)
        best = None
        for text, conf, (x0, y0, x1, y1) in find_text_boxes(img, scale):
            for target in targets:
                sim = similar(text, target)
                sim -= 0.01 * abs(len(re.sub(r"\W", "", text)) - len(re.sub(r"\W", "", target)))
                if sim >= min_sim and (best is None or sim > best[0]):
                    best = (sim, (x0, y0, x1, y1), text, target)
        if not best:
            return None
        W, H = self.mon["width"], self.mon["height"]
        ax, ay = self.mon["left"] + area[0] * W, self.mon["top"] + area[1] * H
        x0, y0, x1, y1 = best[1]
        box = (int(ax + x0), int(ay + y0), int(ax + x1), int(ay + y1))
        return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2, best[2], best[3], box

    def find_in_area(self, area):
        """Loose area that may contain text anywhere (spawn banner). Slower."""
        return find_text(self.grab_area(area))

    def candies(self, area, tries=3):
        """(current, max) from the candy counter, or None if it can't be read."""
        for _ in range(tries):
            text, conf = self.read_area(area)
            got = parse_candies(text)
            if got:
                return got
        return None


if __name__ == "__main__":
    import sys
    for path in sys.argv[1:]:
        img = cv2.imread(path)
        print(path, "->", read_line(img))
