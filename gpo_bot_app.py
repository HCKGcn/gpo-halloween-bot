"""
gpo_bot_app.py - the GPO Trick-or-Treat Bot app (dark / #ff005f).

Run:    python gpo_bot_app.py
Build:  build_exe.bat  ->  dist/GPOTrickBot.exe  (one file, no Python needed)

Everything the bot needs (template, areas, routes, settings) is saved next to the scripts, or for the
exe in %APPDATA%/GPOTrickBot, seeded from the setup baked in at build time (build_exe.bat).
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import cv2
import keyboard
import mss
import numpy as np

import knock_bot as kb
from find_knock_gui import App as DetectorApp
from notify import SpawnWatcher, png_bytes, send_discord
from text_reader import TextReader

# ----------------------------------------------------------------------------- look
BG = "#0d0d11"
SIDEBAR = "#121218"
CARD = "#18181f"
CARD_HI = "#1f1f28"
BORDER = "#2a2a36"
TEXT = "#ececf2"
MUTED = "#8b8b9c"
ACCENT = "#ff005f"
ACCENT_HI = "#ff3d84"
ACCENT_DIM = "#4a1029"
OK = "#3ddc97"
WARN = "#ffb020"
FONT = "Segoe UI"
MONO = "Consolas"


UI = {"s": 1.0}       # display scaling (1.0 = 100 %, 1.5 = 150 %), set when the window opens


def px(v):
    return int(round(v * UI["s"]))


def lighten(hex_color, amount=0.12):
    c = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    c = [min(255, int(v + (255 - v) * amount)) for v in c]
    return "#%02x%02x%02x" % tuple(c)


class FlatButton(tk.Label):
    """Flat button with hover. kind: accent | ghost | danger"""

    def __init__(self, parent, text, command, kind="ghost", width=None, pad=(16, 8), font_size=10):
        colors = {"accent": (ACCENT, "#ffffff"), "ghost": (CARD_HI, TEXT), "danger": ("#3a1420", "#ff6b8f")}
        self.bg, fg = colors[kind]
        super().__init__(parent, text=text, bg=self.bg, fg=fg, padx=pad[0], pady=pad[1], cursor="hand2",
                         font=(FONT, font_size, "bold" if kind == "accent" else "normal"))
        if width:
            self.config(width=width)
        self.command = command
        self.enabled = True
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=lighten(self.bg, 0.15)))
        self.bind("<Leave>", lambda e: self.config(bg=self.bg if self.enabled else BORDER))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, on):
        self.enabled = on
        self.config(bg=self.bg if on else BORDER, cursor="hand2" if on else "arrow")

    def recolor(self, bg, fg=None):
        self.bg = bg
        self.config(bg=bg)
        if fg:
            self.config(fg=fg)


class Toggle(tk.Canvas):
    """iOS-style switch."""

    def __init__(self, parent, value=False, command=None, bg=CARD):
        super().__init__(parent, width=px(44), height=px(24), bg=bg, highlightthickness=0, cursor="hand2")
        self.value = value
        self.command = command
        self.bind("<Button-1>", self.flip)
        self.draw()

    def draw(self):
        self.delete("all")
        on = self.value
        fill = ACCENT if on else BORDER
        q = px
        self.create_oval(q(2), q(2), q(22), q(22), fill=fill, outline=fill)
        self.create_oval(q(22), q(2), q(42), q(22), fill=fill, outline=fill)
        self.create_rectangle(q(12), q(2), q(32), q(22), fill=fill, outline=fill)
        x = 24 if on else 4
        self.create_oval(q(x), q(4), q(x + 16), q(20), fill="#ffffff", outline="#ffffff")

    def flip(self, _=None):
        self.value = not self.value
        self.draw()
        if self.command:
            self.command(self.value)

    def set(self, v):
        self.value = bool(v)
        self.draw()


def card(parent, **kw):
    f = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1, **kw)
    return f


def label(parent, text, size=10, color=TEXT, bold=False, bg=CARD, **kw):
    return tk.Label(parent, text=text, bg=bg, fg=color, font=(FONT, size, "bold" if bold else "normal"),
                    anchor="w", justify="left", **kw)


def entry(parent, value="", width=30, show=None):
    e = tk.Entry(parent, bg=BG, fg=TEXT, insertbackground=ACCENT, relief="flat", width=width,
                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                 font=(FONT, 10), show=show)
    e.insert(0, value)
    return e


class ScrollPage(tk.Frame):
    """A page that scrolls when the window is too small for it (mouse wheel or the thin bar)."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.bar = tk.Canvas(self, width=px(8), bg=BG, highlightthickness=0, bd=0)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y", padx=(0, px(4)))
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self.win, width=e.width), self._update()))
        for w in (self.canvas, self.inner, self.bar):
            w.bind("<Enter>", lambda e: self._wheel(True))
            w.bind("<Leave>", lambda e: self._wheel(False))
        self.bar.bind("<B1-Motion>", self._drag)
        self.bar.bind("<Button-1>", self._drag)

    def _wheel(self, on):
        if on:
            self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units"))
            self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        else:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.unbind_all(ev)

    def _update(self):
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        self.canvas.configure(yscrollcommand=self._draw_bar)
        self._draw_bar(*self.canvas.yview())

    def _draw_bar(self, first, last):
        first, last = float(first), float(last)
        self.bar.delete("all")
        if last - first >= 0.999:
            self.canvas.yview_moveto(0)
            return                              # everything fits: no bar
        h = self.bar.winfo_height()
        self.bar.create_rectangle(px(2), first * h, px(7), last * h, fill=BORDER, outline=BORDER)

    def _drag(self, e):
        h = max(1, self.bar.winfo_height())
        first, last = self.canvas.yview()
        self.canvas.yview_moveto(max(0.0, e.y / h - (last - first) / 2))


# ----------------------------------------------------------------------------- app
class BotApp:
    def __init__(self, root):
        self.root = root
        root.title("GPO Trick Bot")
        root.configure(bg=BG)
        # Windows display scaling (125 %, 150 % ...) makes the text bigger: size the window to match,
        # but never bigger than the screen. Pages scroll if it's still too small.
        try:
            UI["s"] = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:
            UI["s"] = 1.0
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(px(1080), int(sw * 0.92)), min(px(720), int(sh * 0.88))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(px(820), w), min(px(520), h))

        self.cfg = kb.load_cfg()
        self.q = queue.Queue()
        kb.LOG_SINKS.append(lambda line: self.q.put(("log", line)))
        kb.LOG_SINKS.append(self.log_to_file)

        self.bot = None
        self.worker = None
        self.busy = None            # name of a running setup task (recording / test)
        self.cancel_flag = False
        self.alive = True
        self.run_started = None
        self.run_total = 0.0
        self.watcher = None
        self.want_run = False
        self.test_bot = None
        self.last_hourly = time.time()
        self.last_candies = None

        self.build()
        self.show("Dashboard")
        self.refresh_setup()
        self.apply_watcher()
        try:
            self._last_toggle = 0.0

            def on_toggle_key(_e):
                if time.time() - self._last_toggle > 0.6:      # ignore key auto-repeat
                    self._last_toggle = time.time()
                    self.root.after(0, self.toggle_run)
            # on_press_key fires regardless of other held keys; add_hotkey('f1') didn't while the bot held W
            keyboard.on_press_key(self.cfg["toggle_key"], on_toggle_key)
        except Exception as e:
            self.log_line(f"Couldn't register {self.cfg['toggle_key']}: {e}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        self.tick()
        self.log_line(f"Ready. {self.cfg['toggle_key'].upper()} or the Start button runs the bot.")

    # ------------------------------------------------------------------ layout
    def build(self):
        side = tk.Frame(self.root, bg=SIDEBAR, width=px(200))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(22, 26))
        tk.Label(brand, text="GPO", bg=SIDEBAR, fg=ACCENT, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(brand, text="TRICK-OR-TREAT BOT", bg=SIDEBAR, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        tk.Label(brand, text=f"v{kb.VERSION}", bg=SIDEBAR, fg="#5c5c6e", font=(FONT, 8)).pack(anchor="w")

        self.nav = {}
        for name in ("Dashboard", "Setup", "Notifications", "Settings"):
            b = tk.Label(side, text="   " + name, bg=SIDEBAR, fg=MUTED, font=(FONT, 11), anchor="w",
                         padx=14, pady=10, cursor="hand2")
            b.pack(fill="x")
            b.bind("<Button-1>", lambda e, n=name: self.show(n))
            b.bind("<Enter>", lambda e, w=b: w.config(fg=TEXT))
            b.bind("<Leave>", lambda e, w=b, n=name: w.config(fg=TEXT if self.current == n else MUTED))
            self.nav[name] = b

        foot = tk.Frame(side, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=18, pady=18)
        self.side_dot = tk.Label(foot, text="●", bg=SIDEBAR, fg=MUTED, font=(FONT, 12))
        self.side_dot.pack(side="left")
        self.side_status = tk.Label(foot, text="Stopped", bg=SIDEBAR, fg=MUTED, font=(FONT, 10))
        self.side_status.pack(side="left", padx=6)
        tk.Label(side, text=f"{self.cfg['toggle_key'].upper()}  start / stop", bg=SIDEBAR, fg="#5c5c6e",
                 font=(FONT, 9)).pack(side="bottom", anchor="w", padx=20)

        self.main = tk.Frame(self.root, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self.pages = {}
        for name, builder in (("Dashboard", self.page_dashboard), ("Setup", self.page_setup),
                              ("Notifications", self.page_notifications), ("Settings", self.page_settings)):
            if name == "Dashboard":          # the dashboard fits itself to the window (the log shrinks)
                f = tk.Frame(self.main, bg=BG)
                builder(f)
            else:                            # the others scroll when the window is too small
                f = ScrollPage(self.main)
                builder(f.inner)
            f.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.pages[name] = f
        self.current = None

    def show(self, name):
        self.current = name
        self.pages[name].tkraise()
        for n, b in self.nav.items():
            on = n == name
            b.config(fg=TEXT if on else MUTED, bg=CARD if on else SIDEBAR,
                     text=("▍ " if on else "   ") + n)
        if name == "Setup":
            self.refresh_setup()

    def header(self, parent, title, sub, reserve=0):
        h = tk.Frame(parent, bg=BG)
        h.pack(fill="x", padx=28, pady=(24, 14))
        tk.Label(h, text=title, bg=BG, fg=TEXT, font=(FONT, 18, "bold")).pack(anchor="w")
        s = tk.Label(h, text=sub, bg=BG, fg=MUTED, font=(FONT, 10), anchor="w", justify="left")
        s.pack(anchor="w", fill="x")
        h.bind("<Configure>", lambda e: s.config(wraplength=max(px(300), e.width - px(reserve))))
        return h

    # ------------------------------------------------------------------ dashboard
    def page_dashboard(self, p):
        h = self.header(p, "Dashboard", "Laps, knocks and everything the bot is doing.", reserve=280)
        right = tk.Frame(h, bg=BG)
        right.place(relx=1, rely=0.5, anchor="e")
        self.status_pill = tk.Label(right, text="  STOPPED  ", bg=CARD_HI, fg=MUTED, font=(FONT, 9, "bold"),
                                    padx=8, pady=6)
        self.status_pill.pack(side="left", padx=10)
        self.run_btn = FlatButton(right, "▶  Start", self.toggle_run, kind="accent", pad=(22, 9), font_size=11)
        self.run_btn.pack(side="left")

        grid = tk.Frame(p, bg=BG)
        grid.pack(fill="x", padx=28)
        self.stat_vals = {}
        stats = [("laps", "Laps"), ("knocks", "Knocks"), ("rate", "Knocks / hour"), ("candies", "Candies"),
                 ("shop_runs", "Shop runs"), ("chests_opened", "Chests opened"), ("uptime", "Run time"),
                 ("spawns", "Spawns / items")]
        for i, (key, name) in enumerate(stats):
            c = card(grid)
            c.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 10, 0), pady=(0, 10))
            label(c, name.upper(), 8, MUTED, bold=True).pack(anchor="w", padx=16, pady=(14, 0))
            v = label(c, "–", 20, TEXT, bold=True)
            v.pack(anchor="w", padx=16, pady=(0, 14))
            self.stat_vals[key] = v
        for col in range(4):
            grid.columnconfigure(col, weight=1)
        self.spawns_seen = 0
        self.items_seen = 0

        logc = card(p)
        logc.pack(fill="both", expand=True, padx=28, pady=(4, 24))
        top = tk.Frame(logc, bg=CARD)
        top.pack(fill="x", padx=16, pady=(12, 6))
        label(top, "ACTIVITY", 8, MUTED, bold=True).pack(side="left")
        FlatButton(top, "Clear", lambda: self.logbox.delete("1.0", "end"), pad=(10, 3), font_size=9).pack(side="right")
        self.logbox = tk.Text(logc, bg=BG, fg="#c9c9d6", insertbackground=ACCENT, relief="flat", wrap="word",
                              font=(MONO, 9), padx=12, pady=10, highlightthickness=0, borderwidth=0)
        self.logbox.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.logbox.tag_config("accent", foreground=ACCENT)
        self.logbox.tag_config("ok", foreground=OK)
        self.logbox.tag_config("warn", foreground=WARN)
        self.logbox.tag_config("dim", foreground=MUTED)

    def log_line(self, line):
        if not line.startswith("["):
            line = f"[{time.strftime('%H:%M:%S')}] {line}"
        tag = None
        low = line.lower()
        if "===" in line:
            tag = "accent"
        elif "knocked" in low and "no prompt" not in low or "bought" in low or "opened chest" in low:
            tag = "ok"
        elif any(w in low for w in ("couldn't", "failed", "no prompt", "error", "lost", "warning", "never")):
            tag = "warn"
        self.logbox.insert("end", line[:11], "dim")
        self.logbox.insert("end", line[11:] + "\n", tag)
        if int(self.logbox.index("end-1c").split(".")[0]) > 1500:
            self.logbox.delete("1.0", "300.0")
        self.logbox.see("end")

    # ------------------------------------------------------------------ setup
    def page_setup(self, p):
        from find_knock_gui import _seed_dir
        seed = _seed_dir()
        has = {k: os.path.exists(os.path.join(seed, *f.split("/"))) for k, f in
               (("route", "wasd_route/route.json"), ("shop", "wasd_route/shop.json"))}
        has["template"] = os.path.exists(kb.resource(kb.KNOCK_DEFAULT))
        self.header(p, "Setup", "Green = ready. 'Built in' steps came with the app: redo them only if they "
                                "don't work for you." if any(has.values()) else "Do these once. Green = done.")

        def pre(key, builtin, own):
            return ("Built in. " + builtin) if has[key] else own
        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True, padx=28, pady=(0, 20))

        tip = tk.Frame(wrap, bg=ACCENT_DIM)
        tip.pack(fill="x", pady=(0, 10))
        label(tip, "Before you start, in GPO's settings:", 10, TEXT, bold=True, bg=ACCENT_DIM).pack(
            anchor="w", padx=16, pady=(10, 0))
        t = label(tip, "Turn OFF 'Show Local Overhead' (your name over your head hides the Knock prompt).  "
                       "Turn OFF 'Auto Run' (the routes were recorded without it).",
                  10, TEXT, bg=ACCENT_DIM)
        t.pack(anchor="w", fill="x", padx=16, pady=(0, 10))
        tip.bind("<Configure>", lambda e: t.config(wraplength=max(px(220), e.width - px(36))))
        self.setup_marks = {}

        def step(key, title, desc, buttons):
            c = card(wrap)
            c.pack(fill="x", pady=(0, 10))
            row = tk.Frame(c, bg=CARD)
            row.pack(fill="x", padx=16, pady=12)
            mark = tk.Label(row, text="●", bg=CARD, fg=ACCENT, font=(FONT, 14))
            mark.pack(side="left", padx=(0, 12))
            self.setup_marks[key] = mark
            wide = len(buttons) > 2            # many buttons: put them on their own row under the text
            if not wide:
                btns = tk.Frame(row, bg=CARD)
                btns.pack(side="right")
            txt = tk.Frame(row, bg=CARD)
            txt.pack(side="left", fill="x", expand=True)
            label(txt, title, 11, TEXT, bold=True).pack(anchor="w")
            d = label(txt, desc, 10, MUTED)
            d.pack(anchor="w", fill="x")
            txt.bind("<Configure>", lambda e, w=d: w.config(wraplength=max(px(220), e.width - 4)))
            if wide:
                btns = tk.Frame(txt, bg=CARD)
                btns.pack(anchor="w", pady=(8, 0))
            for j, (text, cmd, kind) in enumerate(buttons):
                FlatButton(btns, text, cmd, kind=kind, pad=(12, 6), font_size=9).pack(
                    side="left", padx=((0 if wide and j == 0 else 6), 0))

        step("template", "1 · Knock prompt",
             pre("template", "Only if doors aren't found: open the detector, ", "Open the detector, ") +
             "stand at a door, click New template and box the E + 'Knock', then close it (the X saves).",
             [("Open detector", self.open_detector, "accent"), ("Use built-in", self.use_builtin_knock, "danger")])
        step("areas", "2 · Screen areas (automatic)",
             "Nothing to do: the bot finds the candy counter, HUD buttons, message area and hotbar by itself, "
             "at any window size. Only if Diagnostics (Settings) says it can't find one, box it here - "
             "'Use automatic' undoes that.",
             [("Candy counter", lambda: self.pick_area("candy_area"), "ghost"),
              ("Message area", lambda: self.pick_area("banner_area"), "ghost"),
              ("HUD buttons", lambda: self.pick_area("hud_area"), "ghost"),
              ("Hotbar", lambda: self.pick_area("hotbar_slots"), "ghost"),
              ("Use automatic", self.reset_areas, "danger")])
        step("route", "3 · Door route",
             pre("route", "To record your own: ", "") +
             "Respawn, don't touch the camera. F6 on spawn, equip basket, walk "
             "with WASD: F2 at each door, F5 waypoint, F4 undo. After the last door do the death route, then F3.",
             [("Record", lambda: self.run_task("record", kb.record), "accent"),
              ("Cancel", self.cancel_task, "danger")])
        step("shop", "4 · Shop route",
             pre("shop", "To record your own: ", "") +
             "Respawn, F6 on spawn, walk to the witch until the shop pops up, F2, walk into the water, F3.",
             [("Record", lambda: self.run_task("record_shop", kb.record_shop), "accent"),
              ("Cancel", self.cancel_task, "danger")])
        step("tests", "5 · Tests",
             "Respawned at the spawn point, then click a test and switch to Roblox (it waits for focus). "
             "Opening needs 2+ chests, buying needs 250+ candies.",
             [("Open 1 chest", lambda: self.run_test("open", 1), "ghost"),
              ("Buy 1 chest", lambda: self.run_test("shop", 1), "ghost"),
              ("Stop", self.cancel_task, "danger")])

    def refresh_setup(self):
        route_ok = False
        if os.path.exists(kb.ROUTE_JSON):
            try:
                with open(kb.ROUTE_JSON) as f:
                    route_ok = bool(json.load(f).get("death"))
            except Exception:
                pass
        done = {"template": bool(kb.knock_template_source()),
                "areas": True,
                "route": route_ok,
                "shop": os.path.exists(kb.SHOP_JSON),
                "tests": False}
        for k, mark in self.setup_marks.items():
            if k == "tests":
                mark.config(text="◆", fg=MUTED)
            else:
                mark.config(text="✓" if done[k] else "●", fg=OK if done[k] else ACCENT)

    def use_builtin_knock(self):
        """Put your own knock template aside (renamed, not deleted) so only the built-in one is used."""
        from find_knock_gui import TEMPLATE_PATH, META_PATH
        moved = False
        for path in (TEMPLATE_PATH, META_PATH):
            if os.path.exists(path):
                os.replace(path, path + f".old_{time.strftime('%Y%m%d_%H%M%S')}")
                moved = True
        self.drop_bot()
        kb.log("Knock prompt: using the built-in one" + (" (your old template was renamed .old_...)" if moved else ""))
        self.refresh_setup()

    def reset_areas(self):
        for k, v in (("candy_area", None), ("banner_area", None), ("hotbar_slots", None), ("hud_set", False),
                     ("box_sizes", {})):
            self.set_cfg(k, v)
        self.drop_bot()
        self.apply_watcher()
        kb.log("Screen areas: back to automatic.")

    def open_detector(self):
        top = tk.Toplevel(self.root)
        top.configure(bg=BG)
        DetectorApp(top)
        top.bind("<Destroy>", lambda e: self.root.after(300, self.refresh_setup) if e.widget is top else None)

    def pick_area(self, key):
        """Minimize, screenshot, then box the area on the screenshot."""
        self.log_line(f"Switch to Roblox: screenshot in 3 seconds ({key.replace('_', ' ')})")
        self.root.iconify()

        def grab():
            import screen
            with mss.mss() as sct:
                shot = cv2.cvtColor(np.array(sct.grab(screen.capture_rect())), cv2.COLOR_BGRA2BGR)
            self.root.deiconify()
            self.area_picker(key, shot)
        self.root.after(3000, grab)

    def area_picker(self, key, shot):
        H, W = shot.shape[:2]
        s = min(1100 / W, 640 / H)
        small = cv2.resize(shot, (int(W * s), int(H * s)), interpolation=cv2.INTER_AREA)
        top = tk.Toplevel(self.root, bg=BG)
        top.title("Drag a box")
        top.attributes("-topmost", True)
        title = {"candy_area": "Box the candy counter",
                 "banner_area": "Box the message area: wide, from the spawn line down to well below the Safe Zone banner (New Item messages)",
                 "hud_area": "Box the Menu / Backpack / Party / Daily Quests buttons (be alive!)",
                 "hotbar_slots": "Box the hotbar EXACTLY: left edge of slot 1 to right edge of your last slot"}[key]
        label(top, title + ", then click Save.", 11, TEXT, bold=True, bg=BG).pack(anchor="w", padx=14, pady=(12, 6))
        ok, buf = cv2.imencode(".png", small)
        import base64
        photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()))
        cv = tk.Canvas(top, width=small.shape[1], height=small.shape[0], bg=BG, highlightthickness=0, cursor="crosshair")
        cv.pack(padx=14)
        cv.create_image(0, 0, anchor="nw", image=photo)
        cv.photo = photo
        sel = {"start": None, "rect": None, "box": None}

        def press(e):
            sel["start"] = (e.x, e.y)
            if sel["rect"]:
                cv.delete(sel["rect"])
            sel["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT, width=2)

        def drag(e):
            if sel["start"]:
                cv.coords(sel["rect"], sel["start"][0], sel["start"][1], e.x, e.y)

        def release(e):
            x0, y0 = sel["start"]
            x0, x1 = sorted((x0, e.x))
            y0, y1 = sorted((y0, e.y))
            if x1 - x0 > 5 and y1 - y0 > 5:
                sel["box"] = [round(x0 / small.shape[1], 4), round(y0 / small.shape[0], 4),
                              round(x1 / small.shape[1], 4), round(y1 / small.shape[0], 4)]

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)

        def save():
            if not sel["box"]:
                return
            box = sel["box"]
            crop = shot[int(box[1] * H):int(box[3] * H), int(box[0] * W):int(box[2] * W)]
            if key == "hud_area":
                share = kb.hud_share(crop)
                if share < 0.03:
                    kb.log(f"That box has almost no button colour ({share:.3f}). Box the coloured "
                           f"Menu/Backpack/Party/Daily Quests buttons while alive.")
                    return
                self.set_cfg("hud_min", round(share * 0.35, 4))   # dead = ~0, alive = share
                self.set_cfg("hud_set", True)
                kb.log(f"HUD buttons saved: alive = {share:.3f}, counts as dead below {share * 0.35:.3f}")
            self.set_cfg(key, box)
            sizes = dict(self.cfg.get("box_sizes") or {})
            sizes[key] = [W, H]               # a box only fits the window size it was drawn on
            self.set_cfg("box_sizes", sizes)
            if key == "hotbar_slots":
                kb.log("Hotbar box saved (used only when the hotbar isn't found automatically).")
            self.drop_bot()                 # a loaded bot reloads its settings on the next Start
            top.destroy()
            self.refresh_setup()
            if key in ("hud_area", "hotbar_slots"):
                return

            def test():
                from text_reader import find_text, parse_candies, read_line
                if key == "candy_area":
                    t, c = read_line(crop)
                    kb.log(f"Candy counter reads '{t}' -> {parse_candies(t)}")
                else:
                    kb.log(f"Banner strip text: {find_text(crop) or 'nothing right now (fine if no spawn message is up)'}")
                self.apply_watcher()
            threading.Thread(target=test, daemon=True).start()

        bar = tk.Frame(top, bg=BG)
        bar.pack(fill="x", padx=14, pady=12)
        FlatButton(bar, "Save", save, kind="accent").pack(side="right")
        FlatButton(bar, "Cancel", top.destroy).pack(side="right", padx=8)

    def run_task(self, name, fn):
        if self.busy or self.is_running():
            self.log_line("Busy: stop the bot / current task first.")
            return
        self.busy = name
        self.cancel_flag = False
        self.drop_bot()
        self.show("Dashboard")

        def go():
            try:
                fn(should_stop=lambda: self.cancel_flag or not self.alive)
            except Exception as e:
                kb.log(f"Error: {e}")
            finally:
                self.busy = None
                self.root.after(0, self.refresh_setup)
        threading.Thread(target=go, daemon=True).start()

    def run_test(self, what, n):
        if self.busy or self.is_running():
            self.log_line("Busy: stop the bot / current task first.")
            return
        self.busy = "test"
        self.cancel_flag = False
        self.show("Dashboard")

        def go():
            try:
                bot = kb.Bot()
                bot.running = True
                self.test_bot = bot
                kb.log(f"Test: {'open' if what == 'open' else 'buy'} {n} chest(s). Switch to Roblox now.")
                bot.wait_focus()
                if what == "shop":
                    bot.shop_run(n)
                else:
                    bot.open_chests(n)
                    if bot.cfg["store_fruits"]:
                        bot.store_fruits()
                kb.log("Test done.")
            except kb.BotError as e:
                kb.log(str(e))
            except Exception as e:
                kb.log(f"Error: {e}")
            finally:
                kb.release_all()
                self.test_bot = None
                self.busy = None
        threading.Thread(target=go, daemon=True).start()

    def cancel_task(self):
        self.cancel_flag = True
        tb = getattr(self, "test_bot", None)
        if tb:
            tb.running = False

    # ------------------------------------------------------------------ notifications
    def page_notifications(self, p):
        self.header(p, "Notifications", "Get Discord messages about spawns, drops and chests - paste a webhook, "
                                        "then pick what you want to hear about.")
        c = card(p)
        c.pack(fill="x", padx=28, pady=(0, 8))
        label(c, "DISCORD WEBHOOK URL", 8, MUTED, bold=True).pack(anchor="w", padx=16, pady=(14, 4))
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", padx=16, pady=(0, 6))
        FlatButton(row, "Send test", self.test_webhook, pad=(14, 6)).pack(side="right", padx=(8, 0))
        FlatButton(row, "Save", self.save_webhook, kind="accent", pad=(14, 6)).pack(side="right", padx=(8, 0))
        self.hook_entry = entry(row, self.cfg.get("webhook_url", ""), width=20)
        self.hook_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.hook_status = label(c, "Discord: Server settings > Integrations > Webhooks > New webhook > Copy URL",
                                 9, MUTED)
        self.hook_status.pack(anchor="w", padx=16, pady=(0, 10))

        c2 = card(p)
        c2.pack(fill="x", padx=28, pady=(4, 24))
        label(c2, "What to send", 11, TEXT, bold=True).pack(anchor="w", padx=18, pady=(14, 6))
        self.toggles = {}
        for key, title, desc in (
                ("notify_spawns", "Fruit spawn alerts",
                 "Posts 'A Bomb has spawned at Coco Island' when that message shows up in game."),
                ("notify_spawn_image", "Attach a screenshot", "Adds a picture of the message to spawn alerts."),
                ("notify_spawn_mention", "Ping @everyone on spawns", "So your phone actually buzzes."),
                ("notify_items", "Event item drops", "'New Item <Nightfall Aura>' and other rare event drops."),
                ("notify_item_mention", "Ping @everyone on item drops", "You'll want to know right away."),
                ("notify_fruits", "Unboxed fruits", "Posts the fruit from each chest you open (rarity filter below)."),
                ("notify_shop", "Shop runs", "A message each time it buys chests."),
                ("notify_hourly", "Hourly status", "Laps, knocks and candies every hour, so you know it's alive.")):
            row = tk.Frame(c2, bg=CARD)
            row.pack(fill="x", padx=18, pady=5)
            tg = Toggle(row, self.cfg.get(key, False), command=lambda v, k=key: self.on_toggle(k, v))
            tg.pack(side="left", padx=(0, 14))           # switch first: long text can never push it out
            t = tk.Frame(row, bg=CARD)
            t.pack(side="left", fill="x", expand=True)
            label(t, title, 10, TEXT).pack(anchor="w")
            d = label(t, desc, 9, MUTED)
            d.pack(anchor="w", fill="x")
            t.bind("<Configure>", lambda e, w=d: w.config(wraplength=max(px(200), e.width - 4)))
            self.toggles[key] = tg
        row = tk.Frame(c2, bg=CARD)
        row.pack(fill="x", padx=18, pady=(8, 14))
        self.rarity_btn = FlatButton(row, self.cfg.get("min_rarity", "Legendary") + "  ▾", self.cycle_rarity,
                                     kind="ghost", pad=(14, 6))
        self.rarity_btn.pack(side="left", padx=(0, 14))
        t = tk.Frame(row, bg=CARD)
        t.pack(side="left", fill="x", expand=True)
        label(t, "Minimum fruit rarity", 10, TEXT).pack(anchor="w")
        label(t, "Spawns and unboxed fruits below this aren't posted (click to change).", 9, MUTED).pack(anchor="w")

    def cycle_rarity(self):
        from text_reader import RARITY
        cur = self.cfg.get("min_rarity", "Legendary")
        nxt = RARITY[(RARITY.index(cur) + 1) % len(RARITY)] if cur in RARITY else "Legendary"
        self.set_cfg("min_rarity", nxt)
        self.rarity_btn.config(text=nxt + "  ▾")

    def rare_enough(self, rarity):
        from text_reader import rarity_at_least
        return rarity is None or rarity_at_least(rarity, self.cfg.get("min_rarity", "Legendary"))

    def save_webhook(self):
        self.set_cfg("webhook_url", self.hook_entry.get().strip())
        self.hook_status.config(text="Saved.", fg=OK)
        self.apply_watcher()

    def test_webhook(self):
        self.save_webhook()
        url = self.cfg["webhook_url"]

        def go():
            ok, err = send_discord(url, "GPO Trick Bot connected", "Notifications will show up here.")
            self.q.put(("hook", (ok, err)))
        threading.Thread(target=go, daemon=True).start()

    def on_toggle(self, key, value):
        self.set_cfg(key, value)
        if key in ("notify_spawns", "notify_items", "notify_fruits"):
            self.apply_watcher()

    def apply_watcher(self):
        want = self.cfg.get("notify_spawns") or self.cfg.get("notify_items")
        area = self.cfg.get("banner_area")
        size = (self.cfg.get("box_sizes") or {}).get("banner_area")
        if area:
            import screen
            r = screen.capture_rect()
            if not size or abs(r["width"] - size[0]) > 0.02 * size[0] or abs(r["height"] - size[1]) > 0.02 * size[1]:
                area = None                   # drawn on another window size: use the automatic area
        if self.watcher and (not want or self.watcher.area != area):
            self.watcher.stop()
            self.watcher = None
        if want and not self.watcher:
            mon = kb.load_knock_cfg()["monitor"]
            self.watcher = SpawnWatcher(lambda: TextReader(mon), area, self.on_spawn,
                                        log=kb.log, on_item=self.on_item,
                                        known_items=self.cfg.get("known_items"))
            self.watcher.start()
            kb.log("Message watcher on (fruit spawns / item drops).")

    def on_item(self, item, text, img):
        kb.log(f"*** ITEM DROPPED: {item} ***")
        self.q.put(("item", None))
        url = self.cfg.get("webhook_url")
        if url and self.cfg.get("notify_items"):
            ok, err = send_discord(url, f"🎃 ITEM DROPPED: {item}", f"**{item}** just dropped!\n`{text}`",
                                   color=0x3DDC97, image_png=png_bytes(img),
                                   mention="@everyone" if self.cfg.get("notify_item_mention") else "")
            if not ok:
                kb.log(f"Discord error: {err}")

    def on_spawn(self, fruit, place, text, img, rarity=None):
        kb.log(f"FRUIT SPAWN: {fruit}{f' ({rarity})' if rarity else ''} at {place}")
        self.q.put(("spawn", None))
        url = self.cfg.get("webhook_url")
        if url and self.cfg.get("notify_spawns") and self.rare_enough(rarity):
            ok, err = send_discord(url, f"🍎 {fruit} spawned" + (f" · {rarity}" if rarity else ""),
                                   f"**{fruit}** has spawned at **{place}**",
                                   image_png=png_bytes(img) if self.cfg.get("notify_spawn_image") else None,
                                   mention="@everyone" if self.cfg.get("notify_spawn_mention") else "")
            if not ok:
                kb.log(f"Discord error: {err}")

    def on_bot_event(self, kind, info):
        self.q.put(("event", (kind, info)))
        url = self.cfg.get("webhook_url")
        if kind == "shop" and url and self.cfg.get("notify_shop"):
            fields = [("Candies left", info["candies"])] if info.get("candies") is not None else None
            threading.Thread(target=send_discord, daemon=True,
                             args=(url, "Shop run", f"Bought **{info['bought']}** Rare Fruit Chest(s)"),
                             kwargs={"fields": fields}).start()
        if kind == "upgrade" and url and self.cfg.get("notify_shop"):
            threading.Thread(target=send_discord, daemon=True,
                             args=(url, "Upgrade bought", f"**{info['item']}** - it gets equipped after the respawn"),
                             ).start()
        if kind == "unbox" and url and self.cfg.get("notify_fruits") and self.rare_enough(info.get("rarity")):
            img = info.get("image")
            if img is not None and img.shape[1] > 1280:
                img = cv2.resize(img, (1280, int(img.shape[0] * 1280 / img.shape[1])), interpolation=cv2.INTER_AREA)
            threading.Thread(target=send_discord, daemon=True,
                             args=(url, f"🎁 Unboxed {info['fruit']} · {info['rarity']}",
                                   f"A **{info['rarity']}** fruit came out of a chest: **{info['fruit']}**. "
                                   f"Storing it if it's new."),
                             kwargs={"image_png": png_bytes(img) if img is not None else None, "color": 0xFFB020,
                                     "mention": "@everyone" if self.cfg.get("notify_item_mention") else ""}).start()
        if kind == "stuck" and url:   # always sent: the bot stopped and needs you
            threading.Thread(target=send_discord, daemon=True,
                             args=(url, "⚠️ Bot stopped - needs you",
                                   "It couldn't get back to the spawn point after dying, so it stopped instead "
                                   "of wandering. Put the character on the spawn point and press start."),
                             kwargs={"color": 0xFFB020,
                                     "mention": "@everyone" if self.cfg.get("notify_spawn_mention") else ""}).start()

    # ------------------------------------------------------------------ settings
    def page_settings(self, p):
        where = "in %APPDATA%\\GPOTrickBot" if getattr(sys, "frozen", False) else "next to the scripts"
        self.header(p, "Settings", f"Saved {where}. Changes apply the next time you press Start.")
        self.fields = {}

        def section(title, sub=""):
            c = card(p)
            c.pack(fill="x", padx=28, pady=(0, 12))
            label(c, title, 11, TEXT, bold=True).pack(anchor="w", padx=18, pady=(14, 0))
            if sub:
                label(c, sub, 9, MUTED).pack(anchor="w", padx=18)
            body = tk.Frame(c, bg=CARD)
            body.pack(fill="x", padx=18, pady=(8, 14))
            return body

        def fields(body, rows):
            for i, (key, name, hint) in enumerate(rows):
                label(body, name, 10, TEXT).grid(row=i, column=0, sticky="w", pady=5)
                e = entry(body, str(self.cfg.get(key, "")), width=10)
                e.grid(row=i, column=1, sticky="w", padx=16, ipady=4)
                label(body, hint, 9, MUTED).grid(row=i, column=2, sticky="w")
                self.fields[key] = e

        def toggles(body, rows):
            for key, name, hint, cmd in rows:
                r = tk.Frame(body, bg=CARD)
                r.pack(fill="x", pady=5)
                value = (self.cfg.get("capture", "window") == "window") if key == "capture" else self.cfg.get(key, False)
                Toggle(r, value, command=cmd or (lambda v, k=key: (self.set_cfg(k, v), self.drop_bot()))
                       ).pack(side="left", padx=(0, 14))
                txt = tk.Frame(r, bg=CARD)
                txt.pack(side="left", fill="x", expand=True)
                label(txt, name, 10, TEXT).pack(anchor="w")
                if hint:
                    label(txt, hint, 9, MUTED).pack(anchor="w")

        body = section("In the game", "Match these to your Roblox / GPO setup.")
        toggles(body, [
            ("auto_run", "I have auto run (experimental)", "Keep Auto Run off in GPO if you can. If it's on, this "
             "re-times the routes for the faster forward speed (25 instead of 16).", None),
            ("capture", "Watch the Roblox window only (recommended)", "Works at any window size and position. "
             "Off = the whole monitor (old setups).", self.set_capture),
            ("require_focus", "Only act while Roblox is focused", "Pauses when you click into another window.", None),
            ("keep_window", "Restore the Roblox window if it shrinks", "", None)])
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x", pady=(8, 0))
        fields(grid, [("basket_key", "Candy bucket slot", "hotbar number (default 4)"),
                      ("chest_key", "Rare Fruit Chest slot", "hotbar number (default 3)"),
                      ("toggle_key", "Start / stop key", "restart the app after changing")])

        body = section("Candy bucket, shop and chests")
        toggles(body, [
            ("manage_bucket", "Fix the bucket when the candy counter is missing", "Finds it in the Backpack / inventory "
             "and puts it back in its slot.", None),
            ("auto_upgrade", "Auto-upgrade the bucket", "Pumpkin Bag -> Pumpkin Basket -> Candy Corn Basket.", None),
            ("open_chests", "Open the chests it buys", "Always keeps one in the chest slot.", None),
            ("store_fruits", "Store new fruits after opening", "Fruits are lost when you die.", None),
            ("check_backpack", "Also look for fruits in the Backpack", "", None)])
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x", pady=(8, 0))
        fields(grid, [("shop_at", "Go shopping at", "candies (500 = when the bucket is full)")])

        body = section("Timing", "Only change these if something is too fast or too slow for your PC.")
        fields(body, [("after_cutscene", "Wait after a cutscene", "seconds, after the prompt comes back"),
                      ("after_respawn", "Wait after respawning", "seconds, once the HUD is back"),
                      ("death_timeout", "Max time to die", "seconds after the death route, then it retries"),
                      ("cooldown", "House cooldown", "seconds before the same door gives candy again")])

        c = card(p)
        c.pack(fill="x", padx=28, pady=(0, 24))
        bar = tk.Frame(c, bg=CARD)
        bar.pack(fill="x", padx=18, pady=14)
        FlatButton(bar, "Save settings", self.save_settings, kind="accent").pack(side="left")
        FlatButton(bar, "Diagnostics", self.run_diagnostics).pack(side="left", padx=(8, 0))
        FlatButton(bar, "Support bundle", self.make_bundle).pack(side="left", padx=(8, 0))
        FlatButton(bar, "Open folder", lambda: os.startfile(kb.BASE) if hasattr(os, "startfile") else None
                   ).pack(side="left", padx=(8, 0))
        if getattr(sys, "frozen", False):
            FlatButton(bar, "Restore built-in setup", self.restore_builtin, kind="danger").pack(side="left", padx=(8, 0))
        self.settings_status = label(c, "Diagnostics shows what the bot sees and what to fix. Support bundle makes "
                                        "a zip to send when asking for help (no webhook inside).", 9, MUTED)
        self.settings_status.pack(anchor="w", fill="x", padx=18, pady=(0, 14))
        c.bind("<Configure>", lambda e: self.settings_status.config(wraplength=max(px(260), e.width - px(40))))

    def restore_builtin(self):
        if self.is_running() or self.busy:
            self.settings_status.config(text="Stop the bot first.", fg=WARN)
            return
        if not messagebox.askyesno("Restore built-in setup",
                                   "Replace your current settings, routes and template with the setup that was "
                                   "built into this exe?\n\nYour current ones are backed up first. "
                                   "The app closes afterwards; just open it again."):
            return
        from find_knock_gui import restore_seed
        backup = restore_seed()
        messagebox.showinfo("Restored", f"Done. Old setup backed up to:\n{backup}")
        self.close()

    # ------------------------------------------------------------------ support
    def log_to_file(self, line):
        """Everything in the activity log also goes to log.txt (kept under ~2 MB) for support."""
        try:
            path = os.path.join(kb.BASE, "log.txt")
            if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def run_diagnostics(self):
        if self.is_running() or self.busy:
            self.settings_status.config(text="Stop the bot first.", fg=WARN)
            return
        self.settings_status.config(text="Switch to Roblox: checking in 3 seconds...", fg=MUTED)
        self.root.iconify()

        def go():
            time.sleep(3)
            try:
                lines, img = kb.diagnostics()
                self.q.put(("diag", (lines, img)))
            except Exception as e:
                self.q.put(("diag", ([f"FIX  Diagnostics failed: {e}"], None)))
        threading.Thread(target=go, daemon=True).start()

    def show_diagnostics(self, lines, img):
        self.root.deiconify()
        self.settings_status.config(text="")
        for line in lines:
            kb.log("[diag] " + line)
        top = tk.Toplevel(self.root, bg=BG)
        top.title("Diagnostics")
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"{min(px(960), int(sw * 0.9))}x{min(px(760), int(sh * 0.85))}")
        page = ScrollPage(top)
        page.pack(fill="both", expand=True)
        p = page.inner
        label(p, "What the bot sees right now", 13, TEXT, bold=True, bg=BG).pack(anchor="w", padx=16, pady=(14, 2))
        label(p, "Boxes show where the bot looks. Green OK lines are fine; orange FIX lines say what to do.", 10,
              MUTED, bg=BG).pack(anchor="w", padx=16, pady=(0, 8))
        if img is not None:
            import base64
            h, w = img.shape[:2]
            sc = min(px(900) / w, px(460) / h, 1.0)
            small = cv2.resize(img, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".png", small)
            photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()))
            pic = tk.Label(p, image=photo, bg=BG)
            pic.image = photo
            pic.pack(padx=16, anchor="w")
        box = card(p)
        box.pack(fill="x", padx=16, pady=12)
        for line in lines:
            color = OK if line.startswith("OK") else WARN if line.startswith("FIX") else TEXT
            label(box, line, 10, color, wraplength=px(860)).pack(anchor="w", padx=12, pady=2)
        FlatButton(p, "Close", top.destroy).pack(anchor="e", padx=16, pady=(0, 14))

    def make_bundle(self):
        try:
            path = kb.support_bundle()
            self.settings_status.config(text=f"Saved {os.path.basename(path)} (webhook removed) - send that file.",
                                        fg=OK)
            if hasattr(os, "startfile"):
                os.startfile(kb.BASE)
        except Exception as e:
            self.settings_status.config(text=f"Couldn't make the bundle: {e}", fg=WARN)

    def set_capture(self, window_only):
        self.set_cfg("capture", "window" if window_only else "monitor")
        import screen
        screen.configure(self.cfg["capture"])
        self.drop_bot()
        self.settings_status.config(
            text="Changed. Automatic detection adapts by itself; boxes you drew yourself may need redoing "
                 "(Setup > Use automatic).", fg=WARN)

    def save_settings(self):
        for key, e in self.fields.items():
            raw = e.get().strip()
            old = self.cfg.get(key)
            try:
                val = type(old)(raw) if isinstance(old, (int, float)) and not isinstance(old, bool) else raw
            except ValueError:
                self.settings_status.config(text=f"'{raw}' isn't a valid number for {key}", fg=WARN)
                return
            self.cfg[key] = val
        self.write_cfg()
        self.drop_bot()
        self.settings_status.config(text="Saved. Applies next time you press Start.", fg=OK)

    # ------------------------------------------------------------------ config
    def set_cfg(self, key, value):
        self.cfg = kb.load_cfg()
        self.cfg[key] = value
        self.write_cfg()

    def write_cfg(self):
        with open(kb.BOT_CFG_PATH, "w") as f:
            json.dump(self.cfg, f, indent=2)

    # ------------------------------------------------------------------ running
    def is_running(self):
        return bool(self.bot and self.bot.running)

    def drop_bot(self):
        if self.bot and not self.bot.running:
            self.bot = None

    def toggle_run(self):
        if self.busy:
            self.log_line("A setup task is running; finish or cancel it first.")
            return
        if self.is_running():
            self.bot.running = False
            kb.release_all()
            self.run_total += time.time() - (self.run_started or time.time())
            self.run_started = None
            self.log_line("Stopped.")
            self.set_status("stopped")
            return
        if self.bot and self.bot.resume:
            i, at = self.bot.resume
            n = len(self.bot.points)
            where = ("the death route (all doors done)" if i >= n else
                     (f"door {self.bot.door_no[i]}" if i in self.bot.door_no else f"waypoint {i}")
                     + (f", {at:.1f}s into the walk there" if at else ""))
            ans = messagebox.askyesnocancel(
                "Continue or restart?",
                f"The bot was stopped in the middle of lap {self.bot.lap_no}.\n\n"
                f"Yes = continue from {where}\n(only if the character hasn't moved since)\n\n"
                f"No = start a fresh lap\n(respawn first, so you're on the spawn point)\n\n"
                f"Cancel = don't start", parent=self.root)
            if ans is None:
                return
            if ans:
                self.bot.resume_plan = self.bot.resume
                self.log_line(f"Continuing from {where}.")
            else:
                self.bot.resume = None
        if not (self.bot and self.bot.resume_plan):
            self.log_line("Starting - the character should be alive on the spawn point (respawn first if you "
                          "stopped it in the middle of a lap).")
        self.set_status("loading")
        self.want_run = True
        if not self.worker or not self.worker.is_alive():
            self.worker = threading.Thread(target=self.work, daemon=True)
            self.worker.start()

    def work(self):
        """Owns the Bot (screen capture objects live in this thread)."""
        while self.alive:
            if getattr(self, "want_run", False):
                self.want_run = False
                if self.bot is None:
                    try:
                        self.bot = kb.Bot()
                        self.bot.on_event = self.on_bot_event
                    except kb.BotError as e:
                        kb.log(str(e))
                        self.q.put(("status", "stopped"))
                        continue
                    except Exception as e:
                        kb.log(f"Couldn't start: {e}")
                        self.q.put(("status", "stopped"))
                        continue
                self.bot.running = True
                self.run_started = time.time()
                self.q.put(("status", "running"))
            if self.bot and self.bot.running:
                try:
                    self.bot.lap()
                except Exception as e:
                    kb.log(f"Error in lap: {e}")
                    time.sleep(2)
                if self.bot and not self.bot.running and self.run_started:   # stopped itself (stuck)
                    self.run_total += time.time() - self.run_started
                    self.run_started = None
                    self.q.put(("status", "stopped"))
            else:
                time.sleep(0.1)

    def set_status(self, state):
        if state == "running":
            self.status_pill.config(text="  ● RUNNING  ", bg=ACCENT_DIM, fg=ACCENT)
            self.run_btn.config(text="■  Stop")
            self.run_btn.recolor(CARD_HI, TEXT)
            self.side_dot.config(fg=ACCENT)
            self.side_status.config(text="Running", fg=TEXT)
        elif state == "loading":
            self.status_pill.config(text="  STARTING…  ", bg=CARD_HI, fg=WARN)
            self.side_dot.config(fg=WARN)
            self.side_status.config(text="Starting", fg=MUTED)
        else:
            self.status_pill.config(text="  STOPPED  ", bg=CARD_HI, fg=MUTED)
            self.run_btn.config(text="▶  Start")
            self.run_btn.recolor(ACCENT, "#ffffff")
            self.side_dot.config(fg=MUTED)
            self.side_status.config(text="Stopped", fg=MUTED)

    # ------------------------------------------------------------------ loops
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.log_line(data)
                elif kind == "status":
                    self.set_status(data)
                elif kind == "spawn":
                    self.spawns_seen += 1
                elif kind == "item":
                    self.items_seen += 1
                elif kind == "event" and data[0] == "candies":
                    self.last_candies = f"{data[1]['candies']}/{data[1]['max']}"
                elif kind == "diag":
                    self.show_diagnostics(*data)
                elif kind == "hook":
                    ok, err = data
                    self.hook_status.config(text="Test message sent ✓" if ok else f"Failed: {err}",
                                            fg=OK if ok else WARN)
        except queue.Empty:
            pass
        if self.alive:
            self.root.after(100, self.poll)

    def tick(self):
        st = self.bot.stats if self.bot else {}
        secs = self.run_total + (time.time() - self.run_started if self.run_started else 0)
        h, m = int(secs // 3600), int(secs % 3600 // 60)
        vals = {"laps": st.get("laps", "–"), "knocks": st.get("knocks", "–"),
                "rate": f"{st.get('knocks', 0) / (secs / 3600):.0f}" if secs > 300 and st else "–",
                "candies": self.last_candies or "–", "shop_runs": st.get("shop_runs", "–"),
                "chests_opened": st.get("chests_opened", "–"), "uptime": f"{h}h {m:02d}m" if secs else "–",
                "spawns": f"{self.spawns_seen} / {self.items_seen}"}
        for k, v in vals.items():
            self.stat_vals[k].config(text=str(v))
        if self.cfg.get("notify_hourly") and self.cfg.get("webhook_url") and time.time() - self.last_hourly > 3600:
            self.last_hourly = time.time()
            fields = [("Laps", vals["laps"]), ("Knocks", vals["knocks"]), ("Candies", vals["candies"]),
                      ("Shop runs", vals["shop_runs"]), ("Run time", vals["uptime"]),
                      ("Status", "running" if self.is_running() else "stopped")]
            threading.Thread(target=send_discord, args=(self.cfg["webhook_url"], "Hourly status"),
                             kwargs={"fields": fields}, daemon=True).start()
        if self.alive:
            self.root.after(1000, self.tick)

    def close(self):
        self.alive = False
        if self.bot:
            self.bot.running = False
        if self.watcher:
            self.watcher.stop()
        kb.release_all()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()


def main():
    try:   # own taskbar identity, so Windows shows our icon and not python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HCK.GPOTrickBot")
    except Exception:
        pass
    root = tk.Tk()
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    try:
        root.iconbitmap(default=os.path.join(here, "icon.ico"))   # default= also covers popup windows
    except Exception:
        pass
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
