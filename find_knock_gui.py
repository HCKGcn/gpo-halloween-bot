"""
find_knock_gui.py - GUI version of the GPO "Knock" prompt detector (run it from PyCharm or: python find_knock_gui.py).

One-time setup (in a terminal):
    pip install mss opencv-python numpy

What you get:
    - Live preview of your screen showing exactly what the detector sees
      (green box = prompt found, red box = best guess below threshold, orange box = search region)
    - Start / Stop scanning
    - "New template": minimizes this window, waits, grabs your screen, then you drag a box
      around the E key + "Knock" to save it
    - "Set search region": drag a box on the preview to limit where it looks (faster)
    - Threshold slider, monitor picker, always-on-top
    - Works at any resolution (auto-scales the template, see find_knock.py notes)
    - Settings are saved in knock_config.json next to this file
"""
import base64
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
import mss
import numpy as np

import screen

try:  # input: pip install pydirectinput keyboard
    import pydirectinput
    pydirectinput.PAUSE = 0
except Exception:
    pydirectinput = None
try:
    import keyboard  # global hotkeys (work while the game has focus)
except Exception:
    keyboard = None


SEED_FILES = ["knock_template.png", "knock_template.json", "knock_config.json", "bot_config.json"]
SEED_DIRS = ["wasd_route"]


def _seed_dir():
    return os.path.join(getattr(sys, "_MEIPASS", ""), "seed")


def _app_root():
    root = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "GPOTrickBot")
    os.makedirs(root, exist_ok=True)
    return root


def _fill_from_seed(root, seed):
    """Copy what's MISSING from the setup baked into the exe. Never overwrites what the exe saved:
    settings you changed in the app (webhook, toggles...) always win over a new build."""
    import shutil
    for n in SEED_FILES:
        src, dst = os.path.join(seed, n), os.path.join(root, n)
        if not os.path.exists(src):
            continue
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        elif n.endswith(".json"):
            try:                                   # add new keys only, keep every existing value
                with open(src) as f:
                    new = json.load(f)
                with open(dst) as f:
                    have = json.load(f)
                added = {k: v for k, v in new.items() if k not in have}
                if added:
                    have.update(added)
                    with open(dst, "w") as f:
                        json.dump(have, f, indent=2)
            except Exception:
                pass
    for n in SEED_DIRS:
        src, dst = os.path.join(seed, n), os.path.join(root, n)
        if not os.path.isdir(src):
            continue
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(src):
            if not os.path.exists(os.path.join(dst, f)):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))


def restore_seed():
    """Settings > Restore built-in setup: back up the current data, then use the exe's built-in setup."""
    import shutil
    root, seed = _app_root(), _seed_dir()
    if not os.path.isdir(seed):
        return None
    backup = os.path.join(root, "backup_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(backup, exist_ok=True)
    for n in SEED_FILES + SEED_DIRS:
        src = os.path.join(root, n)
        if os.path.exists(src):
            shutil.move(src, os.path.join(backup, n))
    _fill_from_seed(root, seed)
    return backup


def _data_dir():
    """Where settings, template and routes live.
    Script: next to the .py files.  Exe: %APPDATA%\\GPOTrickBot (kept between versions), with anything
    missing filled in from the setup baked into the exe."""
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    root = _app_root()
    if os.path.isdir(_seed_dir()):
        _fill_from_seed(root, _seed_dir())
    return root


BASE = _data_dir()
TEMPLATE_PATH = os.path.join(BASE, "knock_template.png")
META_PATH = os.path.join(BASE, "knock_template.json")
CONFIG_PATH = os.path.join(BASE, "knock_config.json")

PREVIEW_W, PREVIEW_H = 960, 540
WIDE_SCALES = [round(0.35 * (3 / 0.35) ** (i / 24), 3) for i in range(25)]


# ----------------------------------------------------------------------------- vision
def prep(img, mode="bright"):
    """Turn an image into what we actually match on.
    bright = only the near-white pixels (the E key outline + 'Knock' text) -> ignores the background
    edges  = Canny edges
    gray   = plain grayscale"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    if mode == "gray":
        return gray
    if mode == "edges":
        return cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    g = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.threshold(g, 185, 255, cv2.THRESH_BINARY)[1]


def match_at_scales(frame_edges, tmpl, scales, mode="bright"):
    best = (-1.0, None, 1.0)
    fh, fw = frame_edges.shape
    for s in scales:
        t = cv2.resize(tmpl, None, fx=s, fy=s,
                       interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
        th, tw = t.shape[:2]
        if th < 8 or tw < 8 or th >= fh or tw >= fw:
            continue
        te = prep(t, mode)
        if te.sum() == 0 or te.std() == 0:
            continue
        res = cv2.matchTemplate(frame_edges, te, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        if mv > best[0]:
            best = (mv, ml, s)
    return best


def narrow(center, spread=0.08, n=5):
    return [round(center * (1 - spread + 2 * spread * i / (n - 1)), 4) for i in range(n)]


def png_b64(img_bgr):
    ok, buf = cv2.imencode(".png", img_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    return base64.b64encode(buf.tobytes())


def fit_preview(img):
    h, w = img.shape[:2]
    s = min(PREVIEW_W / w, PREVIEW_H / h)
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA), s


# ----------------------------------------------------------------------------- scanner thread
class Scanner(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.active = False
        self.alive = True
        self.monitor = 1
        self.threshold = 0.55
        self.mode = "bright"
        self.region = None  # (x0, y0, x1, y1) fractions or None
        self.tmpl = None
        self.ref_h = None
        self.reload_flag = True
        self.lock = threading.Lock()
        self.result = None
        self.counter = 0

    def load_template(self):
        self.tmpl = cv2.imread(TEMPLATE_PATH)
        try:
            with open(META_PATH) as f:
                self.ref_h = json.load(f)["ref_height"]
        except Exception:
            self.ref_h = None

    def get(self):
        with self.lock:
            return self.result

    def run(self):
        locked_scale, misses = None, 0
        with mss.mss() as sct:
            while self.alive:
                if not self.active:
                    time.sleep(0.05)
                    continue
                if self.reload_flag:
                    self.load_template()
                    locked_scale, misses = None, 0
                    self.reload_flag = False
                t0 = time.time()
                try:
                    frame = cv2.cvtColor(np.array(sct.grab(screen.capture_rect())), cv2.COLOR_BGRA2BGR)
                except Exception:
                    time.sleep(0.2)
                    continue
                fh, fw = frame.shape[:2]

                score, loc, scale, found = -1.0, None, 1.0, False
                ox = oy = 0
                region = self.region
                if self.tmpl is not None:
                    search = frame
                    if region:
                        x0, y0, x1, y1 = region
                        ox, oy = int(x0 * fw), int(y0 * fh)
                        search = frame[oy:int(y1 * fh), ox:int(x1 * fw)]
                    mode = self.mode
                    edges = prep(search, mode)
                    if locked_scale:
                        score, loc, scale = match_at_scales(edges, self.tmpl, narrow(locked_scale), mode)
                    else:
                        guess = (fh / self.ref_h) if self.ref_h else 1.0
                        score, loc, scale = match_at_scales(edges, self.tmpl, narrow(guess, 0.1, 5), mode)
                        if score < self.threshold:
                            s2 = match_at_scales(edges, self.tmpl, WIDE_SCALES, mode)
                            if s2[0] > score:
                                score, loc, scale = s2
                    found = score >= self.threshold
                    if found:
                        locked_scale, misses = scale, 0
                    else:
                        misses += 1
                        if locked_scale and misses > 30:
                            locked_scale = None

                # ---- draw overlay
                view = frame
                lw = max(2, fw // 640)
                if region:
                    cv2.rectangle(view, (int(region[0] * fw), int(region[1] * fh)),
                                  (int(region[2] * fw), int(region[3] * fh)), (0, 140, 255), lw)
                box = None
                if loc is not None and self.tmpl is not None:
                    th, tw = self.tmpl.shape[:2]
                    p1 = (ox + loc[0], oy + loc[1])
                    p2 = (p1[0] + int(tw * scale), p1[1] + int(th * scale))
                    col = (0, 220, 0) if found else (0, 0, 255)
                    cv2.rectangle(view, p1, p2, col, lw)
                    cv2.putText(view, f"{score:.2f}", (p1[0], max(20, p1[1] - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, max(0.7, fw / 1800), col, lw)
                    box = (p1[0] + (p2[0] - p1[0]) // 2, p1[1] + (p2[1] - p1[1]) // 2)
                small, pscale = fit_preview(view)
                self.counter += 1
                with self.lock:
                    self.result = dict(id=self.counter, png=png_b64(small), pscale=pscale,
                                       score=score, found=found, scale=scale, res=(fw, fh),
                                       center=box, has_tmpl=self.tmpl is not None,
                                       locked=locked_scale is not None,
                                       fps=1.0 / max(1e-3, time.time() - t0))
                time.sleep(max(0, 0.05 - (time.time() - t0)))


# ----------------------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title("GPO Knock Detector")
        self.cfg = dict(monitor=1, threshold=0.55, region=None, topmost=False, mode="bright", hide=True)
        try:
            with open(CONFIG_PATH) as f:
                self.cfg.update(json.load(f))
        except Exception:
            pass

        self.scanner = Scanner()
        self.scanner.monitor = self.cfg["monitor"]
        self.scanner.threshold = self.cfg["threshold"]
        self.scanner.mode = self.cfg["mode"]
        self.scanner.region = tuple(self.cfg["region"]) if self.cfg["region"] else None
        self.scanner.start()

        self.mode = "live"  # live | freeze_template | freeze_region
        self.frozen = None
        self.frozen_scale = 1.0
        self.last_id = -1
        self.photo = None
        self.thumb = None
        self.drag = None
        self.rect_id = None
        self.next_press = 0.0

        # --- top bar
        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        self.btn_run = ttk.Button(top, text="Start scanning", command=self.toggle_scan)
        self.btn_run.pack(side="left", padx=2)
        ttk.Button(top, text="New template", command=self.new_template).pack(side="left", padx=2)
        ttk.Button(top, text="Set search region", command=self.set_region).pack(side="left", padx=2)
        ttk.Button(top, text="Clear region", command=self.clear_region).pack(side="left", padx=2)

        self.topmost = tk.BooleanVar(value=self.cfg["topmost"])
        ttk.Checkbutton(top, text="Always on top", variable=self.topmost,
                        command=self.apply_topmost).pack(side="right", padx=6)
        self.hide_var = tk.BooleanVar(value=self.cfg["hide"])
        ttk.Checkbutton(top, text="Hide this window from capture", variable=self.hide_var,
                        command=self.apply_hide).pack(side="right", padx=6)

        # --- settings row
        row = ttk.Frame(root, padding=(6, 0))
        row.pack(fill="x")
        ttk.Label(row, text="Monitor").pack(side="left")
        self.mon_var = tk.IntVar(value=self.cfg["monitor"])
        with mss.mss() as s:
            n_mon = max(1, len(s.monitors) - 1)
        ttk.Spinbox(row, from_=1, to=n_mon, width=3, textvariable=self.mon_var,
                    command=self.on_monitor).pack(side="left", padx=(4, 14))
        ttk.Label(row, text="Threshold").pack(side="left")
        self.thr_var = tk.DoubleVar(value=self.cfg["threshold"])
        ttk.Scale(row, from_=0.2, to=0.95, variable=self.thr_var, length=200,
                  command=self.on_threshold).pack(side="left", padx=4)
        self.thr_lbl = ttk.Label(row, text=f"{self.cfg['threshold']:.2f}", width=5)
        self.thr_lbl.pack(side="left")
        ttk.Label(row, text="  Match mode").pack(side="left")
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        cb = ttk.Combobox(row, textvariable=self.mode_var, values=["bright", "edges", "gray"],
                          width=7, state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", self.on_mode)
        ttk.Label(row, text="  Template:").pack(side="left")
        self.thumb_lbl = ttk.Label(row)
        self.thumb_lbl.pack(side="left", padx=4)

        # --- auto-knock row
        arow = ttk.Frame(root, padding=(6, 4))
        arow.pack(fill="x")
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(arow, text="Auto-press E when prompt found  (F8 = toggle, works in-game)",
                        variable=self.auto_var).pack(side="left")
        ttk.Label(arow, text="   Wait after knock (s)").pack(side="left")
        self.limbo_var = tk.DoubleVar(value=11.0)
        ttk.Spinbox(arow, from_=1, to=60, increment=0.5, width=5, textvariable=self.limbo_var).pack(side="left", padx=4)
        ttk.Label(arow, text="  Hold E (s)").pack(side="left")
        self.hold_var = tk.DoubleVar(value=0.15)
        ttk.Spinbox(arow, from_=0.05, to=3, increment=0.05, width=5, textvariable=self.hold_var).pack(side="left", padx=4)
        self.act_lbl = ttk.Label(arow, text="", foreground="#06a")
        self.act_lbl.pack(side="left", padx=10)

        # --- canvas
        self.canvas = tk.Canvas(root, width=PREVIEW_W, height=PREVIEW_H, bg="#111", highlightthickness=0)
        self.canvas.pack(padx=6, pady=6)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.create_text(PREVIEW_W // 2, PREVIEW_H // 2, fill="#888", font=("Segoe UI", 14),
                                text="Click 'Start scanning' to see what the detector sees", tags="hint")

        # --- status bar
        bar = ttk.Frame(root, padding=6)
        bar.pack(fill="x")
        self.state_lbl = tk.Label(bar, text="IDLE", width=10, font=("Segoe UI", 16, "bold"),
                                  bg="#555", fg="white")
        self.state_lbl.pack(side="left")
        self.info_lbl = ttk.Label(bar, text="", font=("Consolas", 10))
        self.info_lbl.pack(side="left", padx=10)

        self.apply_topmost()
        self.root.update()
        self.apply_hide()
        self.refresh_thumb()
        if keyboard is not None:
            try:
                keyboard.add_hotkey("f8", lambda: self.root.after(0, self.toggle_auto))
            except Exception:
                pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    # --- settings
    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())

    def apply_hide(self):
        """Exclude this window from screen capture (Windows 10 2004+), so the detector
        sees the game underneath instead of its own preview."""
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11 if self.hide_var.get() else 0x0)
            if not ok and self.hide_var.get():
                self.info_lbl.config(text="Could not hide window from capture. Move it to another monitor.")
        except Exception:
            pass

    def on_mode(self, _=None):
        self.scanner.mode = self.mode_var.get()
        self.scanner.reload_flag = True

    def roblox_focused(self):
        """Only send keys when the Roblox window is in the foreground."""
        try:
            import ctypes
            u = ctypes.windll.user32
            buf = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(u.GetForegroundWindow(), buf, 256)
            return "roblox" in buf.value.lower()
        except Exception:
            return True

    def press_e(self):
        pydirectinput.keyDown("e")
        time.sleep(float(self.hold_var.get()))
        pydirectinput.keyUp("e")

    def toggle_auto(self):
        self.auto_var.set(not self.auto_var.get())
        self.act_lbl.config(text="Auto-knock ON" if self.auto_var.get() else "Auto-knock OFF")

    def on_monitor(self):
        self.scanner.monitor = int(self.mon_var.get())

    def on_threshold(self, _=None):
        v = round(float(self.thr_var.get()), 2)
        self.scanner.threshold = v
        self.thr_lbl.config(text=f"{v:.2f}")

    def refresh_thumb(self):
        img = cv2.imread(TEMPLATE_PATH)
        if img is None:
            self.thumb_lbl.config(text="(none yet - click New template)", image="")
            return
        h = 36
        s = h / img.shape[0]
        small = cv2.resize(img, (max(1, int(img.shape[1] * s)), h), interpolation=cv2.INTER_AREA)
        self.thumb = tk.PhotoImage(data=png_b64(small))
        self.thumb_lbl.config(image=self.thumb, text="")

    # --- scanning
    def toggle_scan(self):
        self.scanner.active = not self.scanner.active
        self.btn_run.config(text="Stop scanning" if self.scanner.active else "Start scanning")
        if not self.scanner.active:
            self.state_lbl.config(text="IDLE", bg="#555")

    # --- template capture
    def new_template(self):
        was_active = self.scanner.active
        self.scanner.active = False
        self.btn_run.config(text="Start scanning")
        self.state_lbl.config(text="WAIT", bg="#a60")
        self._cd(5, was_active)

    def _cd(self, n, was_active):
        if n > 0:
            self.info_lbl.config(text=f"Switch to the game and stand at a door... capturing in {n}s")
            if n == 5:
                self.root.iconify()
            self.root.after(1000, lambda: self._cd(n - 1, was_active))
            return
        try:
            with mss.mss() as sct:
                frame = cv2.cvtColor(np.array(sct.grab(screen.capture_rect())), cv2.COLOR_BGRA2BGR)
        finally:
            self.root.deiconify()
        self.frozen = frame
        small, self.frozen_scale = fit_preview(frame)
        self.show_png(png_b64(small))
        self.mode = "freeze_template"
        self.state_lbl.config(text="SELECT", bg="#06a")
        self.info_lbl.config(text="Drag a box around the E key + 'Knock' to save it as the template.")

    # --- region
    def set_region(self):
        self.scanner.active = True
        self.btn_run.config(text="Stop scanning")
        res = self.scanner.get()
        if not res:
            self.info_lbl.config(text="Start scanning first, then click Set search region.")
            return
        self.mode = "freeze_region"
        self.frozen_scale = res["pscale"]
        self.state_lbl.config(text="SELECT", bg="#06a")
        self.info_lbl.config(text="Drag a box on the preview where the prompt can appear.")

    def clear_region(self):
        self.scanner.region = None
        self.cfg["region"] = None

    # --- mouse
    def on_press(self, e):
        if self.mode == "live":
            return
        self.drag = (e.x, e.y)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#0f0", width=2)

    def on_motion(self, e):
        if self.drag and self.rect_id:
            self.canvas.coords(self.rect_id, self.drag[0], self.drag[1], e.x, e.y)

    def on_release(self, e):
        if not self.drag:
            return
        x0, y0 = self.drag
        x1, y1 = e.x, e.y
        self.drag = None
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 < 6 or y1 - y0 < 6:
            return
        s = self.frozen_scale
        if self.mode == "freeze_template":
            fx0, fy0, fx1, fy1 = int(x0 / s), int(y0 / s), int(x1 / s), int(y1 / s)
            crop = self.frozen[fy0:fy1, fx0:fx1]
            cv2.imwrite(TEMPLATE_PATH, crop)
            with open(META_PATH, "w") as f:
                json.dump({"ref_height": self.frozen.shape[0], "ref_width": self.frozen.shape[1]}, f)
            self.scanner.reload_flag = True
            self.refresh_thumb()
            self.info_lbl.config(text=f"Template saved ({crop.shape[1]}x{crop.shape[0]}). Press Start scanning.")
        elif self.mode == "freeze_region":
            res = self.scanner.get()
            fw, fh = res["res"]
            pw, ph = fw * s, fh * s
            reg = (max(0, x0 / pw), max(0, y0 / ph), min(1, x1 / pw), min(1, y1 / ph))
            self.scanner.region = reg
            self.cfg["region"] = list(reg)
            self.info_lbl.config(text="Search region set (orange box).")
        self.mode = "live"
        self.state_lbl.config(text="IDLE", bg="#555")

    # --- display
    def show_png(self, b64):
        self.photo = tk.PhotoImage(data=b64)
        self.canvas.delete("hint")
        self.canvas.delete("img")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="img")
        self.canvas.tag_lower("img")

    def poll(self):
        if self.mode == "live" and self.scanner.active:
            r = self.scanner.get()
            if r and r["id"] != self.last_id:
                self.last_id = r["id"]
                self.show_png(r["png"])
                if r["found"] and self.auto_var.get() and time.time() >= self.next_press:
                    if pydirectinput is None:
                        self.act_lbl.config(text="pip install pydirectinput first")
                    elif not self.roblox_focused():
                        self.act_lbl.config(text="Roblox isn't the focused window - not pressing")
                    else:
                        self.press_e()
                        self.next_press = time.time() + float(self.limbo_var.get())
                        self.act_lbl.config(text=f"Pressed E at {time.strftime('%H:%M:%S')}")
                if not r["has_tmpl"]:
                    self.state_lbl.config(text="NO TMPL", bg="#a60")
                    self.info_lbl.config(text="No template yet - click New template.")
                elif r["found"]:
                    self.state_lbl.config(text="FOUND", bg="#1a8a1a")
                else:
                    self.state_lbl.config(text="none", bg="#a22")
                if r["has_tmpl"]:
                    c = r["center"]
                    self.info_lbl.config(
                        text=f"{r['res'][0]}x{r['res'][1]}  score {r['score']:.3f}  scale {r['scale']:.2f}"
                             f"{' [locked]' if r['locked'] else ''}  center {c}  {r['fps']:.0f} fps")
        self.root.after(30, self.poll)

    def on_close(self):
        self.cfg.update(monitor=int(self.mon_var.get()), threshold=round(float(self.thr_var.get()), 2),
                        topmost=self.topmost.get(), mode=self.mode_var.get(), hide=self.hide_var.get())
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.cfg, f)
        except Exception:
            pass
        self.scanner.alive = False
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
