"""
knock_bot.py - GPO trick-or-treat bot: WASD route + screen correction at every door, looping forever.

How it moves:
  1. It replays the keys you pressed while recording (W/A/S/D/space) to get from one point to the next.
  2. At each point it compares the screen with a snapshot taken there while recording. Close up this
     match is very reliable, so if the character is a bit off, it right-clicks (click-to-move) the
     exact recorded spot and checks again. Every door re-anchors the position, so errors never pile up.
  3. After the last door it plays your recorded death route (into the water), waits for the respawn,
     and starts again from the spawn. Respawning always gives the same camera angle, so record and run
     with that angle and don't touch the camera.
A trick that snaps you in front of the door, a click or key eaten by the cutscene lock: all corrected
at the next check.

Needs find_knock_gui.py in the same folder (uses its Knock-prompt template, region, threshold, mode).
Setup:   pip install mss opencv-python numpy pydirectinput keyboard

    python knock_bot.py record   # teach it the route (once)
    python knock_bot.py run      # run laps
    python knock_bot.py check    # debug: which recorded points match the view from where you stand

RECORD
    Respawn first (so the camera is the respawn angle), don't touch the camera, same window size.
    1. Right after spawning, standing on the spawn point, focus Roblox, press F6. Then equip the basket.
    2. Walk to house 1 with W/A/S/D. Stop where the prompt shows, press F2 = door.
       Continue door by door. On a long walk you may press F5 = waypoint somewhere along the way
       (an extra checkpoint, recommended for anything longer than ~5 seconds, like house 9).
       F4 = undo the last point (walk back to the previous point first).
    3. After the last door, do the death route with the keyboard (left, double jumps, right, drop in
       the water), then press F3 = save. Keys after the last F2/F5 become the death route.
    Release your keys before pressing F2/F5/F3. Keys are recorded by physical position (AZERTY is fine).

RUN
    Respawn, don't touch the camera, nothing equipped, press F1 (start/stop, also the emergency stop).
    Only acts while the Roblox window is focused. Each lap: pre_lap steps -> equip basket -> doors ->
    death route -> waits until the HUD vanishes (dead) and comes back (respawned) -> next lap.
    A door still on cooldown: waits if it's ready within cooldown_wait_max seconds, else walks past.
    If a knock does nothing (basket not held), it presses the basket key and retries.

Settings: bot_config.json (missing keys get defaults, the file is rewritten with all keys).
"""
import ctypes
import ctypes.wintypes as wt
import json
import re
import math
import os
import sys
import time

import cv2
import keyboard
import mss
import numpy as np
import pydirectinput

from find_knock_gui import BASE, CONFIG_PATH, META_PATH, TEMPLATE_PATH, match_at_scales, narrow, prep
from text_reader import TextReader
import screen
from screen import auto_message_area

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

VERSION = "1.0.1"
ROUTE_DIR = os.path.join(BASE, "wasd_route")
ROUTE_JSON = os.path.join(ROUTE_DIR, "route.json")
SHOP_JSON = os.path.join(ROUTE_DIR, "shop.json")
BOT_CFG_PATH = os.path.join(BASE, "bot_config.json")
MOVE_KEYS = ("w", "a", "s", "d", "space")
SCAN_TO_KEY = {17: "w", 30: "a", 31: "s", 32: "d", 57: "space"}

DEFAULTS = dict(
    toggle_key="f1",
    click_key="f7",         # while recording: right-click where the mouse is pointing
    basket_key="4",
    pre_lap=[],             # e.g. ["key:2", "wait:0.4", "key:z", "wait:2.5"]
    equip_wait=0.5,
    # --- position correction
    work_h=360,
    crop=0.40,
    match_min=0.25,         # minimum match score to trust the snapshot match at a point
    search_radius=0.25,     # only look for the recorded spot this close to the character (fraction of height)
    click_correction=False, # True = right-click onto the recorded spot when off (made things worse at the respawn camera)
    max_variants=6,         # extra snapshots learned per point for other lighting (day/night/fog)
    moved_min=3.0,          # how much the view must change during a walk to count as "moved"
    lost_score=0.20,        # door with no prompt AND view match below this = suspicious (lost or blocked)
    lost_after=2,           # this many suspicious doors IN A ROW = lost -> bail out
    spawn_min=0.25,         # view match needed to say "this looks like the spawn point" (info only)
    # --- death / respawn detection: the HUD (Menu/Backpack/Party/Daily Quests) vanishes while dead
    hud_area=[0.0, 0.94, 0.24, 1.0],
    hud_min=0.05,           # share of green/yellow button pixels needed to count as "HUD visible"
    death_timeout=45.0,     # after the death route, the HUD must vanish within this (drowning ~15-20s)
    respawn_timeout=40.0,   # then it must come back within this (respawn ~10s)
    after_respawn=1.5,      # settle time once the HUD is back
    death_retries=2,        # didn't die? play the death route again this many times, then stop
    arrive=0.025,           # close enough (fraction of screen height)
    stuck_time=0.7,
    align_timeout=4.0,      # max time spent correcting at one point
    click_hold=0.05,
    ui_mask=[[0.0, 0.76, 1.0, 1.0],
             [0.0, 0.0, 1.0, 0.07],
             [0.0, 0.40, 0.15, 0.48]],
    safe_box=[0.04, 0.09, 0.96, 0.74],
    # --- knocking
    prompt_wait=1.0,
    settle=0.1,
    hold_e=0.15,
    knock_retries=2,
    cutscene_start=2.0,     # after E, the prompt must vanish within this time (= cutscene started)
    cutscene_max=30.0,      # safety: never wait longer than this for the prompt to come back
    after_cutscene=1.5,     # wait this long after the prompt reappears before walking on
    cooldown=180.0,
    cooldown_wait_max=10.0,
    death_wait=28.0,        # (old fixed wait, no longer used: death is detected from the HUD)
    # --- text reading (set the areas with: python knock_bot.py areas)
    candy_area=None,        # where "282/500 Candies" is, fractions of the screen
    banner_area=None,       # optional override; normally found automatically (screen.auto_message_area)
    shop_at=500,            # candies at which a shop run is needed
    chest_price=250,
    shop_item="Rare Fruit Chest",
    shop_buy="chests",      # what shop runs buy: "chests" (Rare Fruit Chests, opened after) or "rerolls"
    reroll_item="Race Reroll",  # the reroll's name in the Halloween shop
    reroll_price=0,         # its price in candies (0 = read it from the shop)
    shop_open_text="Halloween Shop",
    shop_wait=8.0,          # max time for the shop window to show up after the walk
    store_fruits=True,      # after opening chests, store new fruits (they're lost when you die)
    store_words=["Store Fruit", "Store"],
    store_area=[0.2, 0.45, 0.8, 0.93],   # where the store button shows up (above the hotbar)
    hotbar_area=[0.25, 0.86, 0.8, 1.0],  # where the hotbar is (fruits show up there as a "?" icon)
    check_backpack=True,    # also look for fruits in the Backpack (when the hotbar is full)
    icon_min=0.8,           # match needed for the "?" fruit icon
    # --- auto upgrade for new accounts (shop > Candy Buckets), then equip it from the inventory
    auto_upgrade=False,
    upgrade_items={"none": "Pumpkin Bag", "100": "Pumpkin Basket", "250": "Candy Corn Basket"},
    menu_key="m",           # opens the wheel (Inventory...). Sent by key position, so it's ',' on AZERTY
    auto_run=False,         # you have auto run: forward (W) is faster than the other directions
    walk_speed=16.0,        # normal walk speed
    run_speed=25.0,         # forward speed with auto run
    manage_bucket=True,     # no candy counter? find the bucket, equip it from the inventory, drag it to the slot
    hotbar_slots=None,      # optional: the hotbar from slot 1 to the last slot, if it isn't found automatically
    bucket=None,            # the bucket you have (remembered)
    bucket_min=0.84,        # icon match needed for the bucket icons
    tile_min=6.0,           # pixel detail that means "there's an item tile" in the inventory (empty ~1, tile ~20)
    keep_window=True,       # put the Roblox window back to its size if it gets minimized / shrunk
    open_chests=True,       # open the chests bought (at the start of the next lap, 1 always stays)
    chest_key="3",
    chest_click=[0.5, 0.4], # where to click to use the chest (away from the hotbar and buttons)
    confirm_words=["Accept", "Yes", "Proceed", "Confirm", "Open"],
    skip_word="Skip",        # from the end of the death route until you're back at the spawn, in control
    require_focus=True,
    capture="window",       # "window": everything relative to the Roblox window (any size / position)
                            # "monitor": relative to the whole monitor (setups made before window mode)
    # --- Discord (set in the app)
    webhook_url="",
    notify_spawns=False,
    notify_spawn_image=True,
    notify_spawn_mention=False,
    notify_shop=True,
    notify_hourly=False,
    notify_items=True,          # "New Item <...>" (the event items) -> Discord
    notify_item_mention=True,   # @everyone for item drops
    notify_fruits=True,         # chest results + fruit messages -> Discord
    known_items=["Nightfall Aura", "Brand of Sacrifice"],
    min_rarity="Legendary",     # fruit spawns / unboxed fruits below this aren't posted to Discord
)

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


LOG_SINKS = []          # extra receivers for log lines (the app window)


class BotError(Exception):
    """Setup problem the user has to fix (missing template, route...)."""


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    for sink in list(LOG_SINKS):
        try:
            sink(line)
        except Exception:
            pass


def load_cfg():
    cfg = dict(DEFAULTS)
    if os.path.exists(BOT_CFG_PATH):
        with open(BOT_CFG_PATH) as f:
            saved = json.load(f)
        if "capture" not in saved:
            # setups made before window mode existed were boxed on the whole monitor: keep that
            old = any(saved.get(k) for k in ("candy_area", "banner_area", "hud_area", "hotbar_slots"))
            saved["capture"] = "monitor" if old or os.path.exists(TEMPLATE_PATH) else "window"
        cfg.update(saved)
    with open(BOT_CFG_PATH, "w") as f:   # write back so new settings show up in the file
        json.dump(cfg, f, indent=2)
    screen.configure(cfg.get("capture"), load_knock_cfg().get("monitor"))
    return cfg


def load_knock_cfg():
    kc = {"monitor": 1, "threshold": 0.55, "mode": "bright", "region": None}
    try:
        with open(CONFIG_PATH) as f:
            kc.update(json.load(f))
    except Exception:
        pass
    return kc


def focus_roblox():
    """Bring the Roblox window to the front (best effort)."""
    try:
        hwnd = user32.FindWindowW(None, "Roblox")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)                 # restore if minimized
        user32.keybd_event(0x12, 0, 0, 0)          # tap Alt: lets us take the foreground
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        return True
    except Exception:
        return False


def resource(name):
    """A file shipped with the app (inside the exe, or next to the scripts).
    A file with the same name in the data folder overrides it (e.g. your own, sharper icon)."""
    own = os.path.join(BASE, name)
    if os.path.exists(own):
        return own
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, name)


FRUIT_ICON_REF_H = 702      # screen height the bundled "?" icon was cut from
ICON_REF_H = 720            # about the game height the bundled bucket / chest icons were cut from
BUCKETS = ["Candy Corn Basket", "Pumpkin Basket", "Pumpkin Bag"]          # best first
BUCKET_ICONS = {"Candy Corn Basket": "bucket_candycorn.png", "Pumpkin Basket": "bucket_basket.png",
                "Pumpkin Bag": "bucket_bag.png"}
BUCKET_CAP = {"Pumpkin Bag": 100, "Pumpkin Basket": 250, "Candy Corn Basket": 500}


def roblox_hwnd():
    try:
        return user32.FindWindowW(None, "Roblox") or None
    except Exception:
        return None


def window_rect(hwnd):
    import ctypes.wintypes as wt
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def roblox_focused():
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
        return "roblox" in buf.value.lower()
    except Exception:
        return True


def tap(key, hold=0.06):
    pydirectinput.keyDown(key)
    time.sleep(hold)
    pydirectinput.keyUp(key)


HOTBAR_STRIP = [0.0, 0.80, 1.0, 1.0]     # where the hotbar is looked for (bottom of the game)
SLOT_STEP = 1.38                           # distance between slot centres / slot width (same at any size)


def find_slots(strip, H):
    """Hotbar slots in a BGR strip of the bottom of the game: list of (cx, cy, w, h) in strip pixels,
    left to right, or [] if they don't look like a hotbar. H = game height in pixels.
    The slots are dark navy squares (about 5% of the game height), equally spaced and centred
    horizontally; slots hidden by a bright icon are filled in from the spacing."""
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    dark = (hsv[..., 2] < 95).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(dark)
    found = []
    for i in range(1, n):
        x, y, w, h, a = st[i]
        if 0.03 * H < w < 0.08 * H and 0.035 * H < h < 0.08 * H and 0.75 < w / h < 1.35 and a > 0.4 * w * h \
                and _squareish((lab[y:y + h, x:x + w] == i).astype(np.uint8)):
            found.append((x + w / 2, y + h / 2, w, h))
    # on dark ground the slots don't stand out by colour: their frames still show as square outlines
    edges = cv2.dilate(cv2.Canny(cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY), 40, 120), np.ones((3, 3), np.uint8))
    for c in cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[-2]:
        x, y, w, h = cv2.boundingRect(c)
        if 0.035 * H < w < 0.08 * H and 0.035 * H < h < 0.08 * H and 0.75 < w / h < 1.35 \
                and cv2.contourArea(c) > 0.6 * w * h and cv2.contourArea(cv2.convexHull(c)) > 0.85 * w * h:
            found.append((x + w / 2, y + h / 2, w, h))
    rows = []                                             # candidate groups: one row of same-size squares
    for ref in found:
        grp = []
        for f in sorted(f for f in found if abs(f[1] - ref[1]) < ref[3] * 0.3 and abs(f[2] - ref[2]) < ref[2] * 0.2):
            if not grp or f[0] - grp[-1][0] > ref[2] * 0.3:   # same square found twice
                grp.append(f)
        if grp not in rows:
            rows.append(grp)
    good = [g for g in (_slot_row(r, strip.shape[1]) for r in rows) if g]
    if not good:
        return []
    # the hotbar is the lowest row (the haki buttons above it look alike); then the most slots
    low = max(g[0][1] for g in good)
    good = [g for g in good if low - g[0][1] < g[0][3] * 0.5]
    return max(good, key=len)


def _squareish(mask):
    """Slots are (rounded) squares; the haki buttons just above them are circles.
    A circle fills ~78% of its bounding box, a square ~90%+."""
    cnts = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    if not cnts:
        return False
    c = max(cnts, key=cv2.contourArea)
    return cv2.contourArea(cv2.convexHull(c)) > 0.85 * mask.shape[0] * mask.shape[1]


def _slot_row(best, width):
    w = float(np.median([b[2] for b in best]))
    h = float(np.median([b[3] for b in best]))
    cy = float(np.median([b[1] for b in best]))
    gaps = [b[0] - a[0] for a, b in zip(best, best[1:])]
    step = min(gaps) if gaps else w * SLOT_STEP
    if not w * 1.0 <= step <= w * 1.8:
        return None
    xs = [best[0][0]]
    for b in best[1:]:
        k = (b[0] - xs[-1]) / step
        if k < 0.6 or abs(k - round(k)) > 0.2:
            return None                                   # not evenly spaced: not the hotbar
        for j in range(1, int(round(k))):                 # slots in between hidden by their icon
            xs.append(xs[-1] + step)
        xs.append(b[0])
    if len(xs) > 10:
        return None
    if len(xs) >= 2:
        step = (xs[-1] - xs[0]) / (len(xs) - 1)
    mid = (xs[0] + xs[-1]) / 2
    if abs(mid - width / 2) > max(step * 0.6, width * 0.02):
        # the hotbar is always centred. Not centred = empty slots on the right we couldn't see
        # (Backpack open on dark ground): add them if that makes it centred, else it's not the hotbar
        n = 2 * (width / 2 - xs[0]) / step + 1
        if abs(n - round(n)) > 0.25 or not len(xs) < round(n) <= 10:
            return None
        while len(xs) < round(n):
            xs.append(xs[-1] + step)
    return [(x, cy, w, h) for x in xs]


HUD_WORDS = ("Menu", "Backpack", "Party", "Daily Quests")


def find_hud_area(reader):
    """Find the Menu / Backpack / Party / Daily Quests buttons (bottom left) by reading them.
    Returns (area, alive colour share) or None (dead, covered, or not GPO)."""
    from text_reader import find_text_boxes, similar
    search = [0.0, 0.84, 0.5, 1.0]
    img = reader.grab_area(search)
    hits = []
    for text, conf, box in find_text_boxes(img, 1.0):
        if any(similar(text, w) >= 0.7 for w in HUD_WORDS):
            hits.append(box)
    if len(hits) < 2:
        return None
    x0 = min(b[0] for b in hits); y0 = min(b[1] for b in hits)
    x1 = max(b[2] for b in hits); y1 = max(b[3] for b in hits)
    h = y1 - y0
    W, H = reader.mon["width"], reader.mon["height"]
    ih, iw = img.shape[:2]
    px0, py0, px1, py1 = max(0, x0 - 2.5 * h), max(0, y0 - 0.5 * h), min(iw, x1 + 0.5 * h), min(ih, y1 + 0.5 * h)
    share = hud_share(img[int(py0):int(py1), int(px0):int(px1)])
    if share < 0.03:
        return None
    area = [round(search[0] + px0 / W, 4), round(search[1] + py0 / H, 4),
            round(search[0] + px1 / W, 4), round(search[1] + py1 / H, 4)]
    return area, share


def find_candy_area(reader):
    """Find the '123/500 Candies' counter (only shown while the bucket is held). Returns an area
    (a bit wider than the text so bigger numbers still fit) or None."""
    from text_reader import find_text_boxes, parse_candies
    search = [0.15, 0.45, 0.95, 0.97]
    img = reader.grab_area(search)
    W, H = reader.mon["width"], reader.mon["height"]
    for text, conf, (x0, y0, x1, y1) in find_text_boxes(img, 1.5):
        if "and" in text.lower() and parse_candies(text):
            w, h = x1 - x0, y1 - y0
            return [round(max(0, search[0] + (x0 - 0.3 * w) / W), 4), round(max(0, search[1] + (y0 - 0.4 * h) / H), 4),
                    round(min(1, search[0] + (x1 + 0.3 * w) / W), 4), round(min(1, search[1] + (y1 + 0.4 * h) / H), 4)]
    return None


def hud_share(img):
    """Share of the colourful HUD-button pixels (green/olive/yellow) in an image.
    Alive ~0.15-0.25 over the Menu/Backpack/Party/Daily Quests buttons, dead = 0."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s_, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return float(((h >= 15) & (h <= 80) & (s_ >= 90) & (v >= 70)).mean())


def release_hooks(handles, hotkeys):
    """Remove only our own hooks (unhook_all would also kill the app's start/stop key)."""
    for h in handles:
        try:
            keyboard.unhook(h)
        except Exception:
            pass
    for h in hotkeys:
        try:
            keyboard.remove_hotkey(h)
        except Exception:
            pass


def release_all():
    for k in MOVE_KEYS:
        try:
            pydirectinput.keyUp(k)
        except Exception:
            pass


def right_click_here(hold=0.05):
    """Right-click wherever the cursor is, without the mouse moving during the click."""
    import ctypes.wintypes as wt
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    right_click_at(pt.x, pt.y, hold)


def left_click_at(x, y, hold=0.05):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    user32.mouse_event(0x0001, 1, 0, 0, 0)
    user32.mouse_event(0x0001, -1, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(hold)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _move_abs(x, y):
    """A real mouse-move event to screen pixel (x, y) - games that read raw mouse input
    (Roblox drag & drop) only see these, not just a jump of the cursor."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = max(2, user32.GetSystemMetrics(78)), max(2, user32.GetSystemMetrics(79))
    nx = int((x - vx) * 65535 / (vw - 1))
    ny = int((y - vy) * 65535 / (vh - 1))
    user32.mouse_event(0x8001 | 0x4000, nx, ny, 0, 0)     # MOVE | ABSOLUTE | VIRTUALDESK
    user32.SetCursorPos(int(x), int(y))


def drag(src, dst, steps=30, hold=0.35):
    """Press on src, move slowly to dst, wait, release (Roblox drag & drop).
    Moves a few pixels first so the game notices a drag has started."""
    # glide onto the slot and rest there first: Roblox has to see the mouse over the slot
    # BEFORE the button goes down, or it presses on whatever it thought was under the mouse
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    for k in range(1, 11):
        _move_abs(pt.x + (src[0] - pt.x) * k / 10, pt.y + (src[1] - pt.y) * k / 10)
        time.sleep(0.015)
    for d in (2, -2, 1, 0):
        _move_abs(src[0] + d, src[1])
        time.sleep(0.05)
    time.sleep(0.25)
    user32.mouse_event(0x0002, 0, 0, 0, 0)            # left down
    time.sleep(hold)
    for d in (3, 6, 9):                               # start the drag
        _move_abs(src[0] + d, src[1] - d)
        time.sleep(0.04)
    for k in range(1, steps + 1):
        x = src[0] + (dst[0] - src[0]) * k / steps
        y = src[1] + (dst[1] - src[1]) * k / steps
        _move_abs(x, y)
        time.sleep(0.02)
    for d in (2, -2, 0):                              # settle on the slot
        _move_abs(dst[0] + d, dst[1])
        time.sleep(0.06)
    time.sleep(0.25)
    user32.mouse_event(0x0004, 0, 0, 0, 0)            # left up
    time.sleep(0.25)


def scroll_at(x, y, notches=-3):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    for _ in range(abs(notches)):
        user32.mouse_event(0x0800, 0, 0, 120 if notches > 0 else -120, 0)
        time.sleep(0.05)


def right_click_at(x, y, hold):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    user32.mouse_event(0x0001, 1, 0, 0, 0)    # tiny wiggle so Roblox registers the new cursor spot
    user32.mouse_event(0x0001, -1, 0, 0, 0)
    time.sleep(0.02)
    user32.mouse_event(0x0008, 0, 0, 0, 0)    # right down
    time.sleep(hold)
    user32.mouse_event(0x0010, 0, 0, 0, 0)    # right up


# ----------------------------------------------------------------------------- vision helpers
class View:
    """Screen capture + matching in a small, lighting-normalized image."""

    def __init__(self, cfg, monitor):
        self.cfg = cfg
        self.sct = mss.mss()

    @property
    def mon(self):
        return screen.capture_rect()

    def grab_gray(self):
        img = np.array(self.sct.grab(self.mon))
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        h = self.cfg["work_h"]
        s = h / gray.shape[0]
        return cv2.resize(gray, (int(round(gray.shape[1] * s)), h), interpolation=cv2.INTER_AREA)

    def normalize(self, gray):
        g = gray.astype(np.float32)
        hp = g - cv2.GaussianBlur(g, (0, 0), 6)
        energy = np.sqrt(cv2.GaussianBlur(hp * hp, (0, 0), 12)) + 4.0
        out = hp / energy
        H, W = out.shape
        for x0, y0, x1, y1 in self.cfg["ui_mask"]:
            out[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)] = 0
        return out

    def template(self, gray):
        """Patch around the screen center (where the character stood), character + prompt blanked."""
        n = self.normalize(gray)
        H, W = n.shape
        half = int(self.cfg["crop"] * H / 2)
        cx, cy = W // 2, H // 2
        t = n[cy - half:cy + half, cx - half:cx + half].copy()
        # blank the character and the Knock prompt above it
        bx, by0, by1 = int(0.07 * H), int(0.13 * H), int(0.06 * H)
        t[half - by0:half + by1, half - bx:half + bx] = 0
        return t

    def locate_in(self, frame_norm, tmpl, max_off=None):
        """Where is the template's center, relative to the frame center? Returns ((dx, dy) in fractions
        of screen height, score). max_off limits the search to that distance from the center."""
        H, W = frame_norm.shape
        th, tw = tmpl.shape
        ox = oy = 0
        if max_off:
            r = int(max_off * H)
            x0 = max(0, W // 2 - tw // 2 - r)
            y0 = max(0, H // 2 - th // 2 - r)
            frame_norm = frame_norm[y0:min(H, H // 2 + th // 2 + r + 1), x0:min(W, W // 2 + tw // 2 + r + 1)]
            ox, oy = x0, y0
        if th >= frame_norm.shape[0] or tw >= frame_norm.shape[1]:
            return (0.0, 0.0), -1.0
        res = cv2.matchTemplate(frame_norm, tmpl, cv2.TM_CCORR_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        cx = ox + loc[0] + tw / 2
        cy = oy + loc[1] + th / 2
        return ((cx - W / 2) / H, (cy - H / 2) / H), float(score)

    def locate(self, tmpl, max_off=None):
        return self.locate_in(self.normalize(self.grab_gray()), tmpl, max_off)

    def click_offset(self, dx, dy):
        """Right-click the spot (dx, dy) away from the screen center, pulled inside the safe box."""
        m = self.mon
        W, H = m["width"], m["height"]
        sx0, sy0, sx1, sy1 = self.cfg["safe_box"]
        t = 1.0
        while t > 0.1:
            fx = 0.5 + dx * t * H / W
            fy = 0.5 + dy * t
            inside = sx0 <= fx <= sx1 and sy0 <= fy <= sy1
            in_ui = any(x0 <= fx <= x1 and y0 <= fy <= y1 for x0, y0, x1, y1 in self.cfg["ui_mask"])
            if inside and not in_ui:
                right_click_at(m["left"] + fx * W, m["top"] + fy * H, self.cfg["click_hold"])
                return t
            t -= 0.1
        return 0.0


# ----------------------------------------------------------------------------- knock prompt
KNOCK_DEFAULT = "knock_default.png"      # built-in E + Knock prompt, cut from a 1437 px tall game
KNOCK_DEFAULT_H = 1437
KNOCK_DEFAULT_CFG = {"threshold": 0.75, "mode": "gray", "region": [0.2, 0.15, 0.8, 0.85]}


def knock_template_source():
    """'yours + built-in', 'built-in', 'yours' or None."""
    own = os.path.exists(TEMPLATE_PATH)
    builtin = os.path.exists(resource(KNOCK_DEFAULT))
    return " + ".join(n for n, ok in (("yours", own), ("built-in", builtin)) if ok) or None


class _KnockMatcher:
    """One knock-prompt picture and how to look for it."""

    def __init__(self, name, tmpl, ref_h, kc, builtin):
        self.name, self.tmpl, self.ref_h, self.builtin = name, tmpl, ref_h, builtin
        self.threshold, self.mode, self.region = kc["threshold"], kc["mode"], kc["region"]
        self.scale = (screen.capture_rect()["height"] / ref_h) if ref_h else 1.0
        self.locked = False
        self.last_score = 0.0

    @property
    def box(self):
        mon = screen.capture_rect()
        if not self.region:
            return mon
        x0, y0, x1, y1 = self.region
        return {"left": mon["left"] + int(x0 * mon["width"]), "top": mon["top"] + int(y0 * mon["height"]),
                "width": max(1, int((x1 - x0) * mon["width"])), "height": max(1, int((y1 - y0) * mon["height"]))}

    def says_knock(self, img, loc, s):
        """The matched spot really shows the word 'Knock' (right part of the picture)."""
        from text_reader import read_line, similar
        th, tw = int(self.tmpl.shape[0] * s), int(self.tmpl.shape[1] * s)
        crop = img[max(0, loc[1]):loc[1] + th, max(0, loc[0] + int(tw * 0.4)):loc[0] + int(tw * 1.1)]
        if crop.size == 0:
            return False
        text, _ = read_line(crop)
        return any(similar(w, "Knock") >= 0.55 for w in text.split())

    def visible(self, sct):
        img = cv2.cvtColor(np.array(sct.grab(self.box)), cv2.COLOR_BGRA2BGR)
        f = min(1.0, 760 / screen.capture_rect()["height"])   # big windows: match on a smaller copy (faster)
        if f < 1.0:
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        scales = narrow(self.scale, 0.06, 3) if self.locked else narrow(self.scale, 0.25, 7)
        score, loc, s = match_at_scales(prep(img, self.mode), self.tmpl, [x * f for x in scales], self.mode)
        s /= f
        self.last_score = score
        if score >= self.threshold and self.builtin and not self.says_knock(img, loc, s * f):
            self.last_score = min(score, self.threshold - 0.01)
            return False
        if score >= self.threshold:
            self.scale, self.locked = s, True   # size learned: from now on only check around it
            return True
        return False


class Prompt:
    """On-demand Knock-prompt check. Only runs when asked (no background thread), only inside the
    search region, and only at the prompt's known size, so it costs almost nothing.
    Looks with your own template (Setup > Knock prompt) if you made one AND the built-in one (which
    also reads the word 'Knock' before believing a match): a template made on another window size or
    camera can miss prompts the other one sees. Whichever one finds a prompt is tried first after."""

    def __init__(self, kc):
        self.sct = mss.mss()
        self.matchers = []
        own = cv2.imread(TEMPLATE_PATH)
        if own is not None:
            try:
                with open(META_PATH) as f:
                    ref_h = json.load(f)["ref_height"]
            except Exception:
                ref_h = None
            self.matchers.append(_KnockMatcher("yours", own, ref_h, kc, False))
        builtin = cv2.imread(resource(KNOCK_DEFAULT))
        if builtin is not None:
            self.matchers.append(_KnockMatcher("built-in", builtin, KNOCK_DEFAULT_H,
                                               dict(kc, **KNOCK_DEFAULT_CFG), True))
        if not self.matchers:
            raise BotError("No Knock prompt template. Make one first (Setup > Knock prompt).")
        self.best = self.matchers[0]

    @property
    def last_score(self):
        return self.best.last_score

    @property
    def threshold(self):
        return self.best.threshold

    @property
    def region(self):
        return self.best.region

    def visible(self):
        order = [self.best] + [m for m in self.matchers if m is not self.best]
        best_miss = None
        for m in order:
            if m.visible(self.sct):
                self.best = m
                return True
            if best_miss is None or m.last_score - m.threshold > best_miss.last_score - best_miss.threshold:
                best_miss = m
        self.best = best_miss if not self.best.locked else self.best
        return False


# ----------------------------------------------------------------------------- record
def record(should_stop=None):
    cfg = load_cfg()
    kc = load_knock_cfg()
    view = View(cfg, kc["monitor"])
    os.makedirs(ROUTE_DIR, exist_ok=True)
    points = []          # dicts: type, img, keys (the walk from the previous point to this one)
    cur, down = [], {}
    state = {"done": False, "started": False}

    def on_key(e):
        k = SCAN_TO_KEY.get(e.scan_code)
        if k is None or not state["started"]:
            return
        now = time.time()
        if e.event_type == "down":
            if k in down:
                return
            down[k] = now
            cur.append((now, k, "down"))
        elif k in down:
            del down[k]
            cur.append((now, k, "up"))

    def take_leg():
        now = time.time()
        for k in list(down):
            cur.append((now, k, "up"))
        down.clear()
        if not cur:
            return []
        t0 = cur[0][0]
        leg = [[round(t - t0, 3), k, ty] for t, k, ty in cur]
        cur.clear()
        return leg

    def add_point(kind):
        if kind != "start" and not state["started"]:
            log("Press F6 at the spawn point first.")
            return
        leg = take_leg()
        gray = view.grab_gray()
        idx = len(points)
        name = f"p{idx:02d}.png"
        cv2.imwrite(os.path.join(ROUTE_DIR, name), gray)
        points.append({"type": kind, "img": name, "keys": leg})
        doors = sum(1 for p in points if p["type"] == "door")
        label = {"start": "Spawn point", "door": f"Door {doors}", "waypoint": "Waypoint"}[kind]
        walk = f"{leg[-1][0]:.2f}s walk" if leg else "no movement"
        warn = "  <- no keys seen! did you move?" if kind != "start" and not leg else ""
        log(f"{label} saved (point {idx}, {walk}){warn}")

    def start():
        if state["started"]:
            return
        state["started"] = True
        cur.clear()
        down.clear()
        add_point("start")

    def undo():
        if len(points) <= 1:
            log("Nothing to undo (start point stays).")
            return
        p = points.pop()
        try:
            os.remove(os.path.join(ROUTE_DIR, p["img"]))
        except Exception:
            pass
        cur.clear()
        down.clear()
        log(f"Removed point {len(points)}. Go back to point {len(points) - 1} and walk that part again.")

    def finish():
        if not state["started"] or not any(p["type"] == "door" for p in points):
            log("Record at least one door first.")
            return
        death = take_leg()
        if not death:
            log("Do the death route with the keyboard first, then press F3.")
            return
        with open(ROUTE_JSON, "w") as f:
            json.dump({"work_h": cfg["work_h"], "points": points, "death": death,
                       "auto_run": bool(cfg.get("auto_run"))}, f, indent=1)
        doors = sum(1 for p in points if p["type"] == "door")
        log(f"Saved {doors} doors, {len(points) - 1 - doors} waypoints, death route {death[-1][0]:.1f}s.")
        state["done"] = True

    handles = [keyboard.hook(on_key)]
    hot = [keyboard.add_hotkey("f6", start),
           keyboard.add_hotkey("f2", lambda: add_point("door")),
           keyboard.add_hotkey("f5", lambda: add_point("waypoint")),
           keyboard.add_hotkey("f4", undo),
           keyboard.add_hotkey("f3", finish)]
    log("Respawn, stand on the spawn point and press F6, then equip the basket. Walk with W/A/S/D:")
    log("F2 = door, F5 = waypoint, F4 = undo. After the last door do the death route, then press F3.")
    while not state["done"]:
        if should_stop and should_stop():
            log("Recording cancelled, nothing saved.")
            break
        time.sleep(0.1)
    release_hooks(handles, hot)
    return state["done"]


def warp_keys(events, rec_auto, cfg):
    """Re-time a recorded walk for this player's forward speed. With auto run, holding W moves at
    run_speed (25) instead of walk_speed (16), so the same distance takes 16/25 of the time.
    rec_auto: whether the recording was made with auto run. Only the time W is held changes."""
    speed = lambda auto: cfg["run_speed"] if auto else cfg["walk_speed"]
    f = speed(rec_auto) / speed(bool(cfg.get("auto_run")))
    if not events or abs(f - 1) < 1e-6:
        return events
    out, held, last_t, new_t = [], set(), 0.0, 0.0
    for t, k, ty in events:
        new_t += (t - last_t) * (f if "w" in held else 1.0)
        last_t = t
        (held.add if ty == "down" else held.discard)(k)
        out.append([round(new_t, 4), k, ty])
    return out


# ----------------------------------------------------------------------------- run
class Bot:
    def __init__(self):
        self.cfg = load_cfg()
        if not os.path.exists(ROUTE_JSON):
            raise BotError("No door route yet. Record one first (Setup > Door route).")
        with open(ROUTE_JSON) as f:
            data = json.load(f)
        self.cfg["work_h"] = data["work_h"]
        self.points = data["points"]
        self.route_auto = bool(data.get("auto_run"))
        for p in self.points:                     # re-time the walks for this player's speed
            p["raw_keys"] = p["keys"]
            p["keys"] = warp_keys(p["keys"], self.route_auto, self.cfg)
        self.death = warp_keys(data.get("death"), self.route_auto, self.cfg)
        if not self.death:
            raise BotError("This route has no death route. Re-record it (Setup > Door route).")
        kc = load_knock_cfg()
        self.view = View(self.cfg, kc["monitor"])
        # each point can have several snapshots (day / night / fog), learned while running
        self.tmpls = []
        for i, p in enumerate(self.points):
            imgs = [p["img"]] + sorted(f for f in os.listdir(ROUTE_DIR) if f.startswith(f"p{i:02d}_v"))
            self.tmpls.append([self.view.template(cv2.imread(os.path.join(ROUTE_DIR, f), cv2.IMREAD_GRAYSCALE))
                               for f in imgs])
        nvar = sum(len(t) - 1 for t in self.tmpls)
        if nvar:
            log(f"Loaded {nvar} learned extra snapshots (day/night variants).")
        self.door_no, n = {}, 0
        for i, p in enumerate(self.points):
            if p["type"] == "door":
                n += 1
                self.door_no[i] = n
        self.last_knock = {i: 0.0 for i in self.door_no}
        self.first_door = min(self.door_no)
        self.icon = cv2.imread(resource("fruit_icon.png"), cv2.IMREAD_GRAYSCALE)
        self.icon_scale = None
        self.bucket_icons = {n: cv2.imread(resource(f), cv2.IMREAD_GRAYSCALE) for n, f in BUCKET_ICONS.items()}
        self.chest_icons = {"Rare Fruit Chest": cv2.imread(resource("chest_icon.png"), cv2.IMREAD_GRAYSCALE)}
        self.icon_scales = {}
        self._slots, self._slots_good = None, None
        self.auto, self.auto_fail = {}, {}
        self.bad_boxes = set()
        self.alive_at = time.time()         # when we last (re)spawned

        self.keep_one = False       # first chests ever: keep one in the hotbar instead of opening it
        self.unboxed = []
        self.candy_max = None
        self.peli_bought = False
        self.bag_tries = 0
        self.pending_equip = None
        self.had_chest = True
        self.buy_item = self.cfg["shop_item"]
        self.resume = None          # (point, seconds into its walk) where a stopped lap can continue
        self.resume_plan = None     # set by the app when you choose "Continue"
        self.cut = None
        self.wait_first = False
        self.win = None
        h = roblox_hwnd()
        if h and not user32.IsIconic(h):
            r = window_rect(h)
            self.win = {"rect": r, "max": bool(user32.IsZoomed(h)), "full": False}
            try:
                with mss.mss() as sct:
                    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                    self.win["full"] = any(m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] +
                                           m["height"] and r[2] - r[0] >= m["width"] - 2 and r[3] - r[1] >= m["height"] - 2
                                           for m in sct.monitors[1:])
            except Exception:
                pass
        self.suspect = 0
        self.spawn_score, self.spawn_gray = 0.0, None
        self.running = False
        self.lap_no = 0
        self.pos = None
        self.knocked = 0
        self.misses = 0

        self.prompt = Prompt(kc)
        self.text = TextReader(kc["monitor"])
        self.shop = None
        self.btn = None
        self.to_open = 0
        self.stats = {"laps": 0, "knocks": 0, "shop_runs": 0, "chests_bought": 0, "chests_opened": 0}
        self.on_event = None    # callback(kind, info) for the app / Discord
        if os.path.exists(SHOP_JSON):
            with open(SHOP_JSON) as f:
                self.shop = json.load(f)
            auto = bool(self.shop.get("auto_run"))
            self.shop["walk"] = warp_keys(self.shop.get("walk"), auto, self.cfg)
            self.shop["death"] = warp_keys(self.shop.get("death"), auto, self.cfg)
        self.candies = None
        log(f"Ready: {n} doors, {len(self.points) - 1 - n} waypoints, death route {self.death[-1][0]:.1f}s.")

    # --- control
    def event(self, kind, **info):
        if self.on_event:
            try:
                self.on_event(kind, info)
            except Exception as e:
                log(f"(event handler error: {e})")

    def toggle(self):
        self.running = not self.running
        if not self.running:
            release_all()
        log("RUNNING" if self.running else "STOPPED")

    def nap(self, sec):
        end = time.time() + sec
        while self.running and time.time() < end:
            time.sleep(min(0.02, max(0, end - time.time())))

    def wait_focus(self):
        if not self.cfg["require_focus"]:
            return
        warned = False
        while self.running and not roblox_focused():
            if not warned:
                log("Waiting for the Roblox window to be focused...")
                warned = True
            time.sleep(0.3)

    def fresh(self):
        return self.prompt.visible()

    # --- movement
    def play_keys(self, events, start_at=0.0):
        """Replay recorded keys. start_at: begin part-way (keys held at that moment are pressed first).
        Sets self.cut to how far it got if it was stopped, else None."""
        held = set()
        if start_at > 0:
            for t, k, ty in events:
                if t > start_at:
                    break
                (held.add if ty == "down" else held.discard)(k)
            for k in held:
                pydirectinput.keyDown(k)
        t0 = time.time() - start_at
        self.cut = None
        for t, k, ty in events:
            if start_at > 0 and t <= start_at:          # already handled by the held keys above
                continue
            while self.running and time.time() < t0 + t:
                time.sleep(0.002)
            if not self.running:
                self.cut = time.time() - t0
                break
            if ty == "down":
                pydirectinput.keyDown(k)
                held.add(k)
            else:
                pydirectinput.keyUp(k)
                held.discard(k)
        for k in held:
            pydirectinput.keyUp(k)

    def locate_point(self, i, frame=None, radius=None):
        """Best match of point i over all its snapshots."""
        radius = radius or self.cfg["search_radius"]
        if frame is None:
            frame = self.view.normalize(self.view.grab_gray())
        best = ((0.0, 0.0), -1.0)
        for t in self.tmpls[i]:
            r = self.view.locate_in(frame, t, radius)
            if r[1] > best[1]:
                best = r
        return best

    def check_score(self, i):
        """How well the current view matches point i (no clicking). Returns (score, gray frame)."""
        gray = self.view.grab_gray()
        (_, _), score = self.locate_point(i, self.view.normalize(gray))
        return score, gray

    def learn(self, i, gray=None):
        """We're confirmed at point i (its door prompt showed) but the snapshots matched poorly:
        save that view as an extra snapshot for this lighting."""
        variants = self.tmpls[i]
        if len(variants) - 1 >= self.cfg["max_variants"]:
            return
        if gray is None:
            gray = self.view.grab_gray()
        name = f"p{i:02d}_v{len(variants):d}.png"
        cv2.imwrite(os.path.join(ROUTE_DIR, name), gray)
        variants.append(self.view.template(gray))
        log(f"  learned a new snapshot for point {i} (lighting changed), now {len(variants)}")

    def align(self, i):
        """Compare the view with point i's snapshot; click-to-move onto the exact spot if off.
        Returns 'ok' (on the spot), 'far' (spot not recognized), or 'stuck'."""
        t0 = time.time()
        last_click, best, last_progress = 0.0, 9.0, time.time()
        seen = False
        self.last_score = -1.0
        while self.running and time.time() - t0 < self.cfg["align_timeout"]:
            self.wait_focus()
            (dx, dy), score = self.locate_point(i)
            self.last_score = max(self.last_score, score)
            now = time.time()
            if score >= self.cfg["match_min"]:
                seen = True
                d = math.hypot(dx, dy)
                if d <= self.cfg["arrive"]:
                    return "ok"
                if d < best - 0.006:
                    best, last_progress = d, now
                if last_click == 0.0 or (now - last_progress > self.cfg["stuck_time"]
                                         and now - last_click > self.cfg["stuck_time"]):
                    self.view.click_offset(dx, dy)
                    last_click = last_progress = now
            elif not seen and now - t0 > 0.5:
                return "far"
            time.sleep(0.05)
        return "stuck" if seen else "far"

    def recover(self):
        """Find whichever recorded point the view matches best and move onto it."""
        frame = self.view.normalize(self.view.grab_gray())
        best, best_i = -1.0, None
        for i in range(len(self.tmpls)):
            (dx, dy), sc = self.locate_point(i, frame, self.cfg["search_radius"] * 1.5)
            sc -= math.hypot(dx, dy) * 0.3
            if sc > best:
                best, best_i = sc, i
        if best_i is None or best < self.cfg["match_min"] - 0.1:
            return None
        if self.align(best_i) == "ok":
            log(f"Recovered at point {best_i}")
            return best_i
        return None

    # --- knocking
    def knock(self):
        for attempt in range(self.cfg["knock_retries"] + 1):
            if not self.running:
                return False, time.time()
            self.nap(self.cfg["settle"])
            t_press = time.time()
            tap("e", self.cfg["hold_e"])
            while self.running and time.time() - t_press < self.cfg["cutscene_start"]:
                if not self.fresh():
                    return True, t_press
                time.sleep(0.05)
            log(f"  prompt still there after E (attempt {attempt + 1}), pressing the basket key")
            tap(self.cfg["basket_key"])
            self.nap(self.cfg["equip_wait"])
        return False, time.time()

    def wait_cutscene(self, t_press):
        """The prompt vanished when the cutscene started; it comes back when the cutscene ends."""
        while self.running and time.time() - t_press < self.cfg["cutscene_max"]:
            if self.fresh():
                log(f"  cutscene over after {time.time() - t_press:.1f}s")
                self.nap(self.cfg["after_cutscene"])
                return
            time.sleep(0.15)
        log(f"  prompt didn't come back within {self.cfg['cutscene_max']:.0f}s, moving on")

    def prompt_here(self):
        t = time.time()
        while self.running and time.time() - t < self.cfg["prompt_wait"]:
            if self.fresh():
                return True
            time.sleep(0.05)
        log(f"  (best prompt score here {self.prompt.last_score:.2f}, needs {self.prompt.threshold:.2f})")
        return False

    def do_step(self, s):
        kind, _, val = s.partition(":")
        if kind == "wait":
            self.nap(float(val))
        elif kind == "key":
            tap(val)
        elif kind == "click":
            pydirectinput.click()

    # --- one point
    def tiny_view(self):
        return cv2.resize(self.view.grab_gray(), (64, 36), interpolation=cv2.INTER_AREA).astype(np.float32)

    def walk(self, keys, what, start_at=0.0):
        """Play a walk and check the character actually moved (the view follows the character).
        Returns True if it moved. If the keys didn't reach Roblox: refocus and retry once, then stop."""
        if not keys or keys[-1][0] - start_at < 0.5:
            self.play_keys(keys, start_at)
            return self.running
        for attempt in range(2):
            self.wait_focus()
            before = self.tiny_view()
            self.play_keys(keys, start_at if attempt == 0 else 0.0)
            if not self.running:
                return False
            change = float(np.mean(np.abs(self.tiny_view() - before)))
            if change >= self.cfg["moved_min"]:
                return True
            log(f"Didn't move while walking to {what} (view change {change:.1f}, needs {self.cfg['moved_min']}) "
                f"- the keys aren't reaching Roblox.")
            if attempt == 0:
                log("  bringing Roblox to the front and trying that walk again")
                release_all()
                focus_roblox()
        log("STOPPED: movement keys don't reach Roblox. Click into the Roblox window, check you can walk "
            "with W/A/S/D yourself, put the character on the spawn point and press start.")
        self.event("stuck", lap=self.lap_no)
        self.running = False
        release_all()
        return False

    def visit(self, i, start_at=0.0):
        """Walk to point i and knock if it's a door. Returns "ok", "lost" or "stopped"."""
        p = self.points[i]
        what = f"door {self.door_no[i]}" if i in self.door_no else f"waypoint {i}"
        if not self.walk(p["keys"], what, start_at):
            return "stopped"
        if self.cfg["click_correction"]:
            res = self.align(i)
            if res != "ok":
                log(f"Point {i}: position check {res} (best score {self.last_score:.2f}), trusting the walk")
        self.pos = i
        if p["type"] != "door":
            return "ok"
        score, gray = self.check_score(i)
        no = self.door_no[i]
        left = self.cfg["cooldown"] - (time.time() - self.last_knock[i])
        if left > self.cfg["cooldown_wait_max"]:
            log(f"Door {no}: on cooldown ({left:.0f}s left), walking past")
            return "ok"
        if left > 0:
            log(f"Door {no}: ready in {left:.1f}s, waiting")
            self.nap(left + 0.3)
        if not self.prompt_here():
            if score >= self.cfg["lost_score"]:
                log(f"Door {no}: no prompt, but I'm at the right spot (view {score:.2f}) -> occupied, skipped")
                self.suspect = 0
                return "ok"
            self.suspect += 1
            if self.suspect < self.cfg["lost_after"]:
                log(f"Door {no}: no prompt and the view doesn't match (view {score:.2f}) -> probably someone "
                    f"standing in the way, skipped. If the next door fails too, I'll bail.")
                return "ok"
            log(f"Door {no}: no prompt and the view doesn't match, {self.suspect} doors in a row -> I'm off the route")
            return "lost"
        self.suspect = 0
        if score < self.cfg["match_min"] + 0.1:
            self.learn(i, gray)                # confirmed here by the prompt: remember this lighting
        if i == self.first_door and self.spawn_gray is not None and self.spawn_score < self.cfg["match_min"] + 0.1:
            self.learn(0, self.spawn_gray)     # door 1 worked, so the lap started from the real spawn
            self.spawn_gray = None
        ok, t_press = self.knock()
        if not ok:
            log(f"Door {no}: knock failed")
            return "ok"
        self.last_knock[i] = t_press
        self.knocked += 1
        log(f"Door {no}: knocked")
        self.wait_cutscene(t_press)
        if self.cfg["click_correction"]:
            self.align(i)
        return "ok"

    def reverse_keys(self, events):
        """The same walk backwards (W<->S, A<->D, in reverse order)."""
        if not events:
            return []
        flip = {"w": "s", "s": "w", "a": "d", "d": "a", "space": "space"}
        T = events[-1][0]
        out = [[round(T - t, 3), flip.get(k, k), "up" if ty == "down" else "down"] for t, k, ty in events]
        out.sort(key=lambda e: (e[0], e[2] == "down"))
        return out

    def bail(self, i):
        """Lost at point i: walk back to the last good point, die there, respawn."""
        log(f"Bailing out: walking back to point {i - 1}, then the death route")
        self.wait_first = True
        self.event("lost", point=i, door=self.door_no.get(i))
        back = warp_keys(self.reverse_keys(self.points[i]["raw_keys"]), self.route_auto, self.cfg)
        if not self.walk(back, f"point {i - 1} (backwards)"):
            return
        release_all()
        self.nap(0.3)
        self.die(self.death)

    # --- screen areas: found automatically (a box drawn in Setup overrides them)
    def _auto(self, kind, finder, retry=20.0):
        key = (self.text.mon["width"], self.text.mon["height"])
        got = self.auto.get(kind)
        if got and got[0] == key:
            return got[1]
        if time.time() - self.auto_fail.get(kind, 0) < retry:
            return None
        try:
            val = finder(self.text)
        except Exception as e:
            log(f"  (looking for the {kind}: {e})")
            val = None
        if val:
            self.auto[kind] = (key, val)
        else:
            self.auto_fail[kind] = time.time()
        return val

    def own_box_ok(self, key, legacy=True):
        """A box drawn in Setup is used only on the window size it was drawn on (and not after it
        turned out wrong); otherwise the automatic one is used."""
        if not self.cfg.get(key) or key in self.bad_boxes:
            return False
        size = (self.cfg.get("box_sizes") or {}).get(key)
        if not size:
            return legacy                     # drawn before sizes were remembered: trust until it fails
        W, H = self.text.mon["width"], self.text.mon["height"]
        return abs(W - size[0]) <= 0.02 * size[0] and abs(H - size[1]) <= 0.02 * size[1]

    def drop_box(self, key, what):
        if key not in self.bad_boxes:
            self.bad_boxes.add(key)
            log(f"  your {what} box (Setup) doesn't match this window - using the automatic one")

    def hud(self):
        """(area, alive threshold) of the Menu/Backpack/... buttons, or None if not found yet."""
        if self.cfg.get("hud_set") and self.own_box_ok("hud_area"):
            return self.cfg["hud_area"], self.cfg["hud_min"]
        got = self._auto("HUD buttons", find_hud_area)
        if got and "hud_logged" not in self.auto:
            self.auto["hud_logged"] = True
            log(f"Found the HUD buttons (colour {got[1]:.3f})")
        return (got[0], round(got[1] * 0.35, 4)) if got else None

    def hud_area(self):
        h = self.hud()
        return h[0] if h else self.cfg["hud_area"]

    def candy_area(self, find=True):
        if self.own_box_ok("candy_area"):
            return self.cfg["candy_area"]
        if not find:
            got = self.auto.get("candy counter")
            return got[1] if got else None
        return self._auto("candy counter", find_candy_area, retry=0)

    def read_candies(self, tries=2):
        """(current, max) from the candy counter (bucket must be held), finding it if needed."""
        area = self.candy_area()
        got = self.text.candies(area, tries=tries) if area else None
        if not got and area:
            if self.own_box_ok("candy_area"):             # your box reads nothing: try finding it
                auto = find_candy_area(self.text)
                if auto and self.text.candies(auto, tries=1):
                    self.drop_box("candy_area", "candy counter")
                    self.auto["candy counter"] = ((self.text.mon["width"], self.text.mon["height"]), auto)
                    return self.text.candies(auto, tries=tries)
                return None
            self.auto.pop("candy counter", None)          # moved (window resized?): look again
            area = self.candy_area()
            got = self.text.candies(area, tries=1) if area else None
        return got

    def message_area(self):
        # an old message box can't be checked (no message = nothing to read): automatic unless drawn on this size
        return self.cfg["banner_area"] if self.own_box_ok("banner_area", legacy=False) else auto_message_area(self.text.mon)

    def hud_visible(self):
        """True while alive: the bottom-left buttons are on screen. They all vanish while dead."""
        h = self.hud()
        if not h:
            return True, -1.0          # buttons not found yet: assume alive
        share = hud_share(self.text.grab_area(h[0]))
        return share >= h[1], share

    def wait_hud(self, visible, timeout):
        t0 = time.time()
        while self.running and time.time() - t0 < timeout:
            if self.hud_visible()[0] == visible:
                return time.time() - t0
            self.nap(0.4)
        return None

    def die(self, keys):
        """Play a death route and make sure we actually died and respawned.
        Returns True when back alive at the spawn; stops the bot if it can't die."""
        if not self.hud():
            log("(couldn't find the HUD buttons: using the fixed respawn wait)")
            self.play_keys(keys)
            release_all()
            self.nap(self.cfg["death_wait"])
            return True
        for attempt in range(self.cfg["death_retries"] + 1):
            if not self.running:
                return False
            if attempt:
                log(f"Didn't die - playing the death route again ({attempt}/{self.cfg['death_retries']})")
            self.play_keys(keys)
            release_all()
            took = self.wait_hud(False, self.cfg["death_timeout"])
            if took is None:
                continue
            log(f"  dead ({took:.0f}s after the route), waiting for the respawn")
            back = self.wait_hud(True, self.cfg["respawn_timeout"])
            if back is None:
                log("  HUD didn't come back - still waiting a bit")
                back = self.wait_hud(True, 30)
            if back is not None:
                self.alive_at = time.time()                 # HUD is back = alive again
                self.nap(self.cfg["after_respawn"])
                log(f"  respawned ({back:.0f}s)")
                return True
        if self.running:
            log("STUCK: couldn't die / respawn. Stopping so the bot doesn't wander. "
                "Put the character on the spawn point and press start.")
            self.event("stuck", lap=self.lap_no)
            self.running = False
            release_all()
        return False

    def shop_run(self, n=None, mode="chests"):
        """Walk to the witch, buy (chests, or upgrades for a new account), walk into the water.
        Returns True if it ran."""
        if not self.shop:
            log("No shop route recorded (Setup > Shop route). Doing doors.")
            return False
        rerolls = mode == "chests" and self.cfg.get("shop_buy") == "rerolls"
        item = self.cfg["reroll_item"] if rerolls else self.cfg["shop_item"]
        self.buy_item = item
        if mode == "chests" and not rerolls:       # bought chests stack onto one already in the hotbar
            had = self.chest_in_hotbar()
            self.had_chest = True if had is None else had
        price = self.item_price()
        if n is None:
            n = (self.candies or 0) // price
            if n <= 0 and mode != "upgrade":
                log(f"Not enough candies for a {item} ({self.candies or 0} < {price}) - no shop run")
                return False
            if rerolls and not self.cfg.get("reroll_price"):
                n = None                               # exact price is read in the shop
        what = "upgrades" if mode == "upgrade" else f"buying {n if n is not None else 'as many as I can'} x {item}"
        log(f"=== Shop run: {what} ===")
        if not self.walk(self.shop["walk"], "the shop"):
            return True
        t0 = time.time()
        opened = None
        while self.running and time.time() - t0 < self.cfg["shop_wait"]:
            opened = self.text.locate_box([self.cfg["shop_open_text"]])
            if opened:
                break
            self.nap(0.5)
        if not opened:
            log("  shop window never showed up, giving up this run")
            self.debug_shot("shop_not_open")
        else:
            self.shop_title = opened[4]                # the title's box: the list sits right under it
            self.shop_focused = False
            self.list_point = None
            self.shop_focus()
            if mode == "upgrade":
                self.do_upgrades()
            else:
                bought = 0
                self.btn = None
                self.keep_one = not self.had_chest and not rerolls
                if n is None:                          # price not set: read it off the item's card
                    p = self.shop_price(item)
                    if p:
                        self.seen_reroll_price = p
                    n = (self.candies or 0) // (p or price)
                    log(f"  {item}: {p} candies each -> buying {n}" if p else f"  couldn't read the {item} price")
                for _ in range(n):
                    if not self.running or not self.buy_one():
                        break
                    bought += 1
                after = self.read_candies(tries=1)
                log(f"  bought {bought}/{n}" + (f", candies now {after[0]}/{after[1]}" if after else ""))
                self.stats["shop_runs"] += 1
                self.stats["chests_bought"] += bought
                if after:
                    self.candies = after[0]
                self.event("shop", bought=bought, wanted=n, candies=after[0] if after else None, item=item)
                if self.cfg["open_chests"] and not rerolls:
                    keep = 1 if (self.keep_one and bought) else 0
                    self.to_open += bought - keep
                    if keep:
                        log("  no chest in the hotbar before buying -> 1 of these stays in slot "
                            f"{self.cfg['chest_key']} so the next ones stack onto it")
        if self.running:
            self.die(self.shop["death"])
        return True

    def shop_focus(self):
        """Click the shop's title: that focuses the list so the wheel scrolls it."""
        x0, y0, x1, y1 = self.shop_title
        left_click_at((x0 + x1) / 2, (y0 + y1) / 2)
        self.shop_focused = True
        self.nap(0.3)
        return True

    def shop_thumb(self):
        """The shop's scrollbar thumb (thin light bar right of the cards): (cx, top, bottom) on screen."""
        x0, y0, x1, y1 = self.shop_title
        tw, th = x1 - x0, max(8, y1 - y0)
        W, H = self.text.mon["width"], self.text.mon["height"]
        L, T = self.text.mon["left"], self.text.mon["top"]
        sx0, sx1 = max(L, x0 + 2.2 * tw), min(L + W, x0 + 5.5 * tw)
        sy0, sy1 = y1, min(T + H, y1 + 18 * th)
        area = [(sx0 - L) / W, (sy0 - T) / H, (sx1 - L) / W, (sy1 - T) / H]
        img = self.text.grab_area(area)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for thr in (90, 70):
            n, _, st, _ = cv2.connectedComponentsWithStats((g > thr).astype(np.uint8))
            bars = [st[i] for i in range(1, n)
                    if st[i][3] > 1.5 * th and st[i][2] < 0.5 * th and st[i][3] > 3 * st[i][2]]
            if bars:
                bx, by, bw, bh, _ = max(bars, key=lambda r: r[3])
                return sx0 + bx + bw / 2, sy0 + by, sy0 + by + bh
        self.thumb_search = (img, area)
        return None

    def shop_scroll(self, notches, focus=True):
        """Move the shop list by dragging its scrollbar: notches > 0 = all the way up,
        notches < 0 = down by about one screen. Returns False if it couldn't move (or already at the end)."""
        thumb = self.shop_thumb()
        if not thumb:
            if not getattr(self, "wheel_warned", False):
                self.wheel_warned = True
                log("  couldn't find the shop's scrollbar - scrolling with the mouse wheel instead")
                self.debug_shot("shop_no_scrollbar")
                try:
                    os.makedirs(os.path.join(BASE, "debug"), exist_ok=True)
                    cv2.imwrite(os.path.join(BASE, "debug", "shop_scrollbar_search.png"), self.thumb_search[0])
                except Exception:
                    pass
            return self.shop_wheel(notches)
        cx, top, bot = thumb
        mid = (top + bot) / 2
        if notches > 0:
            dst = self.shop_title[3]                   # above the track: clamps to the very top
        else:
            dst = mid + (bot - top) * 0.8              # about one screen down
        drag((cx, mid), (cx, dst))
        self.nap(0.4)
        after = self.shop_thumb()
        return bool(after) and abs(after[1] - top) > 2     # the thumb really moved

    def shop_wheel(self, notches):
        """Scroll the shop list with the mouse wheel (over the middle of the list, after focusing it).
        notches > 0: all the way up; < 0: about one screen down. Returns True if the list moved."""
        x0, y0, x1, y1 = self.shop_title
        tw, th = x1 - x0, max(8, y1 - y0)
        cx, cy = x0 + 2.0 * tw, y1 + 7 * th                   # middle of the cards
        before = cv2.cvtColor(self.text.grab_area([0.25, 0.25, 0.75, 0.75]), cv2.COLOR_BGR2GRAY)
        if not self.shop_focused:
            self.shop_focus()
        scroll_at(cx, cy, 30 if notches > 0 else -5)
        self.nap(0.5)
        after = cv2.cvtColor(self.text.grab_area([0.25, 0.25, 0.75, 0.75]), cv2.COLOR_BGR2GRAY)
        return float(np.mean(cv2.absdiff(before, after))) > 2.0

    def buy_named(self, name):
        """Buy a shop item by its name (Candy Buckets are at the top of the shop)."""
        self.shop_scroll(1)                        # all the way up (Candy Buckets are at the top)
        item = None
        for _ in range(6):
            item = self.text.locate_box([name], min_sim=0.9)   # 0.9: don't mix up Pumpkin Bag / Basket
            if item or not self.shop_scroll(-1):
                break
        if not item:
            log(f"  couldn't find '{name}' in the shop")
            self.debug_shot("shop_no_upgrade")
            return False
        left_click_at(item[0], item[1])
        ok = self.wait_text(["Accept"], 3.0)
        if not ok:
            log(f"  no Accept after clicking {name} (not enough candy/peli, or already owned?)")
            self.debug_shot("shop_upgrade_no_accept")
            return False
        left_click_at(ok[0], ok[1])
        self.nap(1.0)
        log(f"  bought {name}")
        return True

    # ------------------------------------------------------------------ inventory (M > Inventory)
    INV_TABS = ["Loadouts", "Titles", "Grips"]

    def inv_is_open(self):
        return self.text.locate_box(self.INV_TABS, min_sim=0.8) is not None

    def inv_open(self):
        if self.inv_is_open():
            return True
        m = self.text.mon                              # mouse in the middle: the wheel's label then
        _move_abs(m["left"] + m["width"] // 2, m["top"] + m["height"] // 2)   # says "Middle"
        tap(self.cfg["menu_key"])
        self.nap(0.8)
        for _ in range(6):
            if not self.running:
                return False
            # the label names whatever the mouse is over ("Middle", "Inventory", ...); the backpack
            # button is always right under it
            inv = self.text.locate_box(["Inventory", "Middle"])
            if inv and not self.inv_is_open():            # the wheel's label, not the inventory tab
                h = inv[4][3] - inv[4][1]
                left_click_at(inv[0], inv[4][3] + 2.0 * h)  # the backpack button right under the label
                self.nap(1.2)
                if self.inv_is_open():
                    return True
            elif self.inv_is_open():
                return True
            self.nap(0.4)
        log("  couldn't open the inventory (M > Inventory)")
        self.debug_shot("inv_not_open")
        return False

    def inv_close(self):
        for _ in range(3):
            if not self.inv_is_open():
                inv = self.text.locate_box(["Inventory"])  # wheel still showing? close it too
                if inv:
                    tap(self.cfg["menu_key"])
                    self.nap(0.5)
                return True
            tap(self.cfg["menu_key"])
            self.nap(0.7)
        log("  couldn't close the inventory")
        return False

    def inv_search_bar(self):
        box = self.text.locate_box(["Search...", "Search"], min_sim=0.7, scale=2.0)
        if box:
            return box
        clr = self.text.locate_box(["Clear"], scale=2.0)   # old search text in the box
        if clr:
            left_click_at(clr[0], clr[1])
            self.nap(0.5)
            box = self.text.locate_box(["Search...", "Search"], min_sim=0.7, scale=2.0)
            if box:
                return box
        tab = self.text.locate_box(["Inventory"], scale=2.0)   # fall back: the bar sits under the tab
        if tab and self.inv_is_open():
            x0, y0, x1, y1 = tab[4]
            h = y1 - y0
            cx, cy = x0 + 3.4 * h, y1 + 1.8 * h
            return int(cx), int(cy), "", "", (int(x0 + 1.5 * h), int(cy - h / 2), int(x0 + 5.5 * h), int(cy + h / 2))
        return None

    def inv_find(self, name, equip=True):
        """Search the open inventory for `name`. Returns "found" (and equips it), "missing" or "error"."""
        box = self.inv_search_bar()
        if not box:
            log("  couldn't find the inventory search bar")
            self.debug_shot("inv_no_search")
            return "error"
        left_click_at(box[0], box[1])
        self.nap(0.3)
        keyboard.send("ctrl+a")                             # replace whatever was typed before
        self.nap(0.1)
        keyboard.send("backspace")
        self.nap(0.1)
        keyboard.write(name.lower(), delay=0.04)           # layout-aware typing (AZERTY safe)
        self.nap(1.2)
        x0, y0, x1, y1 = box[4]
        bh = max(8, y1 - y0)
        cx, cy = x0 + 2.3 * bh, y1 + 2.4 * bh               # the first result is always here
        W, H = self.text.mon["width"], self.text.mon["height"]
        half = 1.6 * bh
        area = [max(0, (cx - half - self.text.mon["left"]) / W), max(0, (cy - half - self.text.mon["top"]) / H),
                min(1, (cx + half - self.text.mon["left"]) / W), min(1, (cy + half - self.text.mon["top"]) / H)]
        spot = cv2.cvtColor(self.text.grab_area(area), cv2.COLOR_BGR2GRAY)
        detail = float(spot.std())
        if detail < self.cfg["tile_min"]:                  # empty panel there: you don't own it
            log(f"  no {name} in the inventory")
            return "missing"
        if not equip:
            return "found"
        left_click_at(cx, cy)
        self.nap(0.8)
        t0 = time.time()
        while self.running and time.time() - t0 < 3.0:
            eq = self.text.locate_box(["Equip"], min_sim=0.85, scale=2.0)
            if eq:
                left_click_at(eq[0], eq[1])
                self.nap(0.8)
                log(f"  equipped {name}")
                return "found"
            if self.text.locate_box(["Unequip", "Equipped"], min_sim=0.85, scale=2.0):
                log(f"  {name} is already equipped")
                return "found"
            self.nap(0.3)
        log(f"  clicked the {name} but no Equip button showed up")
        self.debug_shot("inv_no_equip")
        return "error"

    def equip_item(self, name):
        """M > Inventory > search > equip. True if equipped."""
        if not self.inv_open():
            return False
        res = self.inv_find(name)
        self.inv_close()
        return res == "found"

    # ------------------------------------------------------------------ candy bucket
    def remember_bucket(self, name):
        if name and self.cfg.get("bucket") != name:
            self.cfg["bucket"] = name
            try:
                with open(BOT_CFG_PATH) as f:
                    disk = json.load(f)
                disk["bucket"] = name
                with open(BOT_CFG_PATH, "w") as f:
                    json.dump(disk, f, indent=2)
            except Exception:
                pass
            log(f"  remembered: you have the {name}")

    def find_bucket(self, area, names=None):
        """Best bucket icon in `area`: (name, (x, y)) or None."""
        return self.find_icons(area, {n: self.bucket_icons.get(n) for n in (names or BUCKETS)}, "bucket")

    def find_icons(self, area, icons, kind):
        """Best match of several icon templates in `area`: (name, (x, y)) or None.
        `kind` keeps a learned scale per kind of icon (bucket / chest)."""
        img = cv2.cvtColor(self.text.grab_area(area), cv2.COLOR_BGR2GRAY)
        W, H = self.text.mon["width"], self.text.mon["height"]
        ox, oy = self.text.mon["left"] + area[0] * W, self.text.mon["top"] + area[1] * H
        known = self.icon_scales.get(kind)
        if known:                          # try the size it had last time first, then every size
            got = self._match_icons(img, ox, oy, icons, [known * f for f in (0.94, 1.0, 1.06)])
            if got:
                return got[:2]
        base = self.text.mon["height"] / ICON_REF_H      # icons grow with the game window
        got = self._match_icons(img, ox, oy, icons, [base * (0.55 + 0.08 * i) for i in range(16)])
        if got:
            self.icon_scales[kind] = got[2]
        return got[:2] if got else None

    def _match_icons(self, img, ox, oy, icons, scales):
        """Best of several icon templates at the given sizes: (name, (x, y), scale) or None."""
        scores = []
        for name, t0 in icons.items():
            if t0 is None:
                continue
            best = (-1, None, None)
            for sc in scales:
                t = cv2.resize(t0, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
                if t.shape[0] >= img.shape[0] or t.shape[1] >= img.shape[1] or t.shape[0] < 10:
                    continue
                r = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
                _, v, _, loc = cv2.minMaxLoc(r)
                if v > best[0]:
                    best = (v, sc, (int(ox + loc[0] + t.shape[1] / 2), int(oy + loc[1] + t.shape[0] / 2)))
            scores.append((best[0], name, best[1], best[2]))
        scores.sort(reverse=True)
        if not scores or scores[0][0] < self.cfg["bucket_min"]:
            return None
        if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.03:
            return None                                   # too close to call
        return scores[0][1], scores[0][3], scores[0][2]

    def hotbar(self, fresh=False):
        """The hotbar right now: (first slot centre x, centre y, slot w, slot h, step) as fractions of the
        game, and the number of slots seen. Found automatically (the hotbar is centred and grows with
        the number of items); falls back to the last good look, then to the boxed hotbar (Setup)."""
        now = time.time()
        if not fresh and self._slots and now - self._slots[0] < 1.5:
            return self._slots[1]
        W, H = self.text.mon["width"], self.text.mon["height"]
        x0, y0, x1, y1 = HOTBAR_STRIP
        try:
            slots = find_slots(self.text.grab_area(HOTBAR_STRIP), H)
        except Exception:
            slots = []
        if slots:
            step = (slots[1][0] - slots[0][0]) if len(slots) > 1 else slots[0][2] * SLOT_STEP
            geom = ((x0 * W + slots[0][0]) / W, (y0 * H + slots[0][1]) / H,
                    slots[0][2] / W, slots[0][3] / H, step / W), len(slots)
            self._slots = (now, geom)
            self._slots_good = geom
            return geom
        if self._slots_good:
            return self._slots_good
        box = self.cfg.get("hotbar_slots") if self.own_box_ok("hotbar_slots") else None
        if box:                                   # boxed by hand: slot 1 .. last slot
            bx0, by0, bx1, by1 = box
            bw, bh = (bx1 - bx0) * W, (by1 - by0) * H
            n = max(1, int(round((bw / bh - 1) / SLOT_STEP + 1)))
            step = (bw - bh) / max(1, n - 1) / W if n > 1 else bh * SLOT_STEP / W
            return (bx0 + bh / 2 / W, (by0 + by1) / 2, bh / W, by1 - by0, step), n
        return None

    def has_hotbar(self):
        return self.hotbar() is not None

    def slot_box(self, key):
        """Screen centre of a hotbar slot, and the area around it (fractions). None if no hotbar."""
        got = self.hotbar()
        if not got:
            return None, None
        (fx, fy, sw, sh, step), _ = got
        idx = 9 if str(key) == "0" else int(key) - 1
        cx = fx + idx * step                      # slots past the last item: where they would appear
        W, H = self.text.mon["width"], self.text.mon["height"]
        center = (int(self.text.mon["left"] + cx * W), int(self.text.mon["top"] + fy * H))
        return center, [max(0, cx - step * 0.6), max(0, fy - sh * 0.8), min(1, cx + step * 0.6), min(1, fy + sh * 0.8)]

    def save_setting(self, key, value):
        try:
            with open(BOT_CFG_PATH) as f:
                disk = json.load(f)
            disk[key] = value
            with open(BOT_CFG_PATH, "w") as f:
                json.dump(disk, f, indent=2)
        except Exception:
            pass

    def backpack(self, open_it):
        hud = self.hud_area()
        area = [max(0, hud[0] - 0.02), max(0, hud[1] - 0.05), min(1, hud[2] + 0.1), 1.0]
        btn = self.text.locate_any(["Backpack"], area=area)
        if not btn:
            return False
        if open_it:
            already = self.text.locate_any(["Backpack"], area=[0, 0, 0.6, 0.35])   # the panel's own title
            if already:
                return True
        left_click_at(btn[0], btn[1])
        self.nap(1.0)
        return True

    def place_icon(self, icons, kind, key, label):
        """Put an item in hotbar slot `key`: if it's not there, open the Backpack, find its icon anywhere
        (hotbar or backpack) and drag it onto the slot. Returns the matched name, or None."""
        target, slot_area = self.slot_box(key)
        if not target:
            log("  (couldn't find the hotbar - box it in Setup > Screen areas > Hotbar slots)")
            return None
        hit = self.find_icons(slot_area, icons, kind)
        if hit:
            return hit[0]
        if not self.backpack(True):
            log("  couldn't open the Backpack")
            return None
        self.nap(0.3)
        # with the Backpack open the hotbar shows all 10 slots and is re-centred: measure it again
        before = self._slots_good
        got = self.hotbar(fresh=True)
        if before and (not got or got[1] != 10):
            # couldn't see all 10: same slot size and spacing as before, 10 of them, centred
            (fx, fy, sw, sh, step), _ = before
            geom = ((0.5 - 4.5 * step, fy, sw, sh, step), 10)
            self._slots, self._slots_good = (time.time() + 30, geom), geom
        target, slot_area = self.slot_box(key)
        hit = self.find_icons([0, 0, 1, 1], icons, kind + " (backpack)")
        placed = None
        if hit:
            for attempt in range(2):
                log(f"  dragging the {hit[0]} to slot {key}" + (" (again, slower)" if attempt else ""))
                drag(hit[1], target, steps=30 + 20 * attempt, hold=0.35 + 0.25 * attempt)
                self.nap(0.8)
                if self.find_icons(slot_area, {hit[0]: icons[hit[0]]}, kind):
                    placed = hit[0]
                    break
                log("  the drag didn't land in the slot")
                self.debug_shot(f"{kind}_drag")
                hit = self.find_icons([0, 0, 1, 1], {hit[0]: icons[hit[0]]}, kind + " (backpack)") or hit
        else:
            log(f"  no {label} in the hotbar / Backpack")
        self.backpack(False)
        self._slots, self._slots_good = None, before    # the hotbar shrinks back when the Backpack closes
        return placed

    def place_bucket(self, names=None):
        name = self.place_icon({n: self.bucket_icons.get(n) for n in (names or BUCKETS)}, "bucket",
                               self.cfg["basket_key"], "candy bucket")
        if name:
            self.remember_bucket(name)
        return bool(name)

    # ------------------------------------------------------------------ rare fruit chests
    def chest_in_hotbar(self):
        got = self.hotbar()
        if not got:
            return None
        (fx, fy, sw, sh, step), n = got
        area = [max(0, fx - step), max(0, fy - sh), min(1, fx + step * max(n, 10)), min(1, fy + sh)]
        return self.find_icons(area, self.chest_icons, "chest") is not None

    def ensure_chest(self):
        """Get a Rare Fruit Chest into the chest slot: hotbar/backpack (drag), else inventory (equip + drag).
        Returns True if one is in the slot now."""
        if self.place_icon(self.chest_icons, "chest", self.cfg["chest_key"], "Rare Fruit Chest"):
            return True
        log("  looking for Rare Fruit Chests in the inventory")
        if not self.equip_item(self.cfg["shop_item"]):
            return False
        self.nap(0.8)
        return bool(self.place_icon(self.chest_icons, "chest", self.cfg["chest_key"], "Rare Fruit Chest"))

    def hold_bucket(self):
        """Press the basket slot and read the counter (pressing again if that put it away)."""
        for _ in range(2):
            tap(self.cfg["basket_key"])
            self.nap(self.cfg["equip_wait"])
            got = self.read_candies(tries=2)
            if got:
                return got
        return None

    def fix_bucket(self):
        """No candy counter: get a bucket into the basket slot.
        Returns "ok", "none" (inventory checked: you own none) or "unknown" (couldn't check)."""
        log("No candy counter -> checking the candy bucket")
        if self.place_bucket():
            return "ok"
        if not self.inv_open():
            return "unknown"
        results = {}
        for name in BUCKETS:                              # best one you own first
            if not self.running:
                break
            log(f"  looking for the {name} in the inventory")
            results[name] = self.inv_find(name)
            if results[name] == "found":
                break
        self.inv_close()
        found = [n for n, r in results.items() if r == "found"]
        if found:
            self.remember_bucket(found[0])
            self.nap(0.8)
            self.place_bucket([found[0]])
            return "ok"
        if len(results) == len(BUCKETS) and all(r == "missing" for r in results.values()):
            return "none"
        return "unknown"

    def do_upgrades(self):
        key = str(self.candy_max) if self.candy_max else "none"
        name = self.cfg["upgrade_items"].get(key)
        if not name:
            log(f"  nothing to upgrade at capacity {key}")
            return
        if self.buy_named(name):
            self.pending_equip = name                       # equipped after the respawn (shop must close first)
            self.event("upgrade", item=name, equipped=None)

    def wait_text(self, targets, timeout):
        t0 = time.time()
        while self.running and time.time() - t0 < timeout:
            hit = self.text.locate_any(targets)
            if hit:
                return hit
            self.nap(0.3)
        return None

    def debug_shot(self, name):
        try:
            os.makedirs(os.path.join(BASE, "debug"), exist_ok=True)
            path = os.path.join(BASE, "debug", f"{time.strftime('%H%M%S')}_{name}.png")
            cv2.imwrite(path, self.text.grab_area([0, 0, 1, 1]))
            log(f"  saved a screenshot: {path}")
        except Exception:
            pass

    def ensure_window(self):
        """Put the Roblox window back if it got minimized or shrunk (that breaks routes, prompt detection,
        HUD check). Moving it - also to another monitor, where fullscreen has another size - is fine:
        the new place is simply remembered."""
        if not self.cfg["keep_window"] or not self.win:
            return
        h = roblox_hwnd()
        if not h:
            return
        rect = window_rect(h)
        iconic = bool(user32.IsIconic(h))

        def size(r):
            return r[2] - r[0], r[3] - r[1]

        def covers_monitor(r):
            cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
            try:
                with mss.mss() as sct:
                    for m in sct.monitors[1:]:
                        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                            return size(r)[0] >= m["width"] - 2 and size(r)[1] >= m["height"] - 2
            except Exception:
                pass
            return False

        if not iconic:
            now_big = bool(user32.IsZoomed(h)) or covers_monitor(rect)
            was_big = self.win["max"] or self.win.get("full", False)
            same_size = all(abs(a - b) <= 8 for a, b in zip(size(rect), size(self.win["rect"])))
            if (was_big and now_big) or (not was_big and same_size):
                if rect != self.win["rect"]:
                    self.win = {"rect": rect, "max": bool(user32.IsZoomed(h)), "full": covers_monitor(rect)}
                return                                   # same window, maybe moved: nothing to fix
        log(f"Roblox window {'minimized' if iconic else f'shrank to {size(rect)[0]}x{size(rect)[1]}'} "
            f"-> putting it back")
        user32.ShowWindow(h, 3 if self.win["max"] else 9)      # maximize / restore
        focus_roblox()
        time.sleep(0.8)
        rect = window_rect(h)
        if self.win.get("full") and not self.win["max"] and not covers_monitor(rect):
            tap("f11")                                       # Roblox's own fullscreen toggle
            time.sleep(1.5)
            rect = window_rect(h)
        if not self.win["max"] and not self.win.get("full") and \
                not all(abs(a - b) <= 8 for a, b in zip(size(rect), size(self.win["rect"]))):
            w, hh = size(self.win["rect"])
            user32.SetWindowPos(h, 0, rect[0], rect[1], w, hh, 0x0004 | 0x0040)   # same size, where it is
            time.sleep(0.8)
            rect = window_rect(h)
        ok = not user32.IsIconic(h) and (covers_monitor(rect) or self.win["max"] or
                                          all(abs(a - b) <= 8 for a, b in zip(size(rect), size(self.win["rect"]))))
        log("  window restored" if ok else "  couldn't restore the window size - fix it by hand")
        self.event("window", ok=ok)

    def fruit_icons(self, area):
        """Screen positions of the "?" fruit icons inside `area`."""
        if self.icon is None:
            return []
        img = cv2.cvtColor(self.text.grab_area(area), cv2.COLOR_BGR2GRAY)
        base = self.text.mon["height"] / FRUIT_ICON_REF_H
        scales = [self.icon_scale] if self.icon_scale else [base * f for f in (0.75, 0.85, 0.95, 1.05, 1.15, 1.3)]
        best_s, best_v, res_best = None, -1, None
        for sc in scales:
            t = cv2.resize(self.icon, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
            if t.shape[0] >= img.shape[0] or t.shape[1] >= img.shape[1] or t.shape[0] < 8:
                continue
            res = cv2.matchTemplate(img, t, cv2.TM_CCOEFF_NORMED)
            v = float(res.max())
            if v > best_v:
                best_s, best_v, res_best, tsz = sc, v, res, t.shape
        if res_best is None or best_v < self.cfg["icon_min"]:
            return []
        self.icon_scale = best_s
        ys, xs = np.where(res_best >= self.cfg["icon_min"])
        pts = []
        for x, y in sorted(zip(xs, ys), key=lambda p: -res_best[p[1], p[0]]):
            if all(abs(x - a) > tsz[1] * 0.6 or abs(y - b) > tsz[0] * 0.6 for a, b in pts):
                pts.append((x, y))
        W, H = self.text.mon["width"], self.text.mon["height"]
        ox, oy = self.text.mon["left"] + area[0] * W, self.text.mon["top"] + area[1] * H
        return [(int(ox + x + tsz[1] / 2), int(oy + y + tsz[0] / 2)) for x, y in pts]

    def store_from(self, area, where):
        """Click each "?" fruit in `area`, press Store if offered, and check it really left."""
        stored, skip = 0, []
        for _ in range(12):
            if not self.running:
                break
            icons = [p for p in self.fruit_icons(area)
                     if all(abs(p[0] - q[0]) > 12 or abs(p[1] - q[1]) > 12 for q in skip)]
            if not icons:
                break
            p = icons[0]
            left_click_at(*p)                      # equip it
            self.nap(0.6)
            hit = self.text.locate_any(self.cfg["store_words"], area=self.cfg["store_area"])
            if not hit:
                log(f"  fruit in the {where} has no Store button (already stored one of those?) - leaving it")
                left_click_at(*p)                  # put it away again
                self.nap(0.3)
                skip.append(p)
                continue
            left_click_at(hit[0], hit[1])
            self.nap(1.2)
            still = any(abs(p[0] - q[0]) <= 12 and abs(p[1] - q[1]) <= 12 for q in self.fruit_icons(area))
            if still:
                log(f"  couldn't store the fruit in the {where} (you can only store one of each) - leaving it")
                left_click_at(*p)
                self.nap(0.3)
                skip.append(p)
                continue
            stored += 1
            log(f"  stored a fruit from the {where}")
        return stored

    def store_fruits(self):
        """Fruits are lost on death: store the new ones (hotbar first, then the backpack)."""
        stored = self.store_from(self.cfg["hotbar_area"], "hotbar")
        if self.cfg["check_backpack"] and self.running:
            hud = self.hud_area()
            area = [max(0, hud[0] - 0.02), max(0, hud[1] - 0.05), min(1, hud[2] + 0.1), 1.0]
            btn = self.text.locate_any(["Backpack"], area=area)
            if btn:
                left_click_at(btn[0], btn[1])      # open
                self.nap(1.0)
                stored += self.store_from([0.05, 0.08, 0.95, 0.85], "backpack")
                left_click_at(btn[0], btn[1])      # close
                self.nap(0.6)
            else:
                log("  (couldn't find the Backpack button to check it)")
        if stored:
            self.stats["fruits_stored"] = self.stats.get("fruits_stored", 0) + stored
            self.event("fruit_stored", count=stored)
        return stored

    def open_chests(self, n):
        """Equip the chest, click the screen, confirm, skip the animation. n times."""
        log(f"Opening {n} chest(s)")
        W, H = self.text.mon["width"], self.text.mon["height"]
        cx = self.text.mon["left"] + self.cfg["chest_click"][0] * W
        cy = self.text.mon["top"] + self.cfg["chest_click"][1] * H
        opened = 0
        for k in range(n):
            if not self.running:
                break
            tap(self.cfg["chest_key"])
            self.nap(0.6)
            left_click_at(cx, cy)
            hit = self.wait_text(self.cfg["confirm_words"], 4.0)
            if not hit:
                log("  no confirmation popup after using the chest")
                self.debug_shot("chest_no_confirm")
                break
            left_click_at(hit[0], hit[1])
            fruit, skipped, shot = None, False, None
            from text_reader import find_text, parse_unbox
            msg_area = self.message_area()
            t0 = time.time()
            while self.running and time.time() - t0 < 12 and not (fruit and skipped):
                if not fruit:                      # "You unboxed a Horo!" shows while it spins
                    for line, conf in find_text(self.text.grab_area(msg_area)):
                        got = parse_unbox(line)
                        if got:
                            fruit = got
                            shot = self.text.grab_area([0, 0, 1, 1])
                            break
                if not skipped:
                    sk = self.text.locate_any([self.cfg["skip_word"]])
                    if sk:
                        left_click_at(sk[0], sk[1])
                        skipped = True
                self.nap(0.3)
            if fruit:
                log(f"  unboxed: {fruit[0]} ({fruit[1]})")
                self.unboxed.append(fruit)
                self.event("unbox", fruit=fruit[0], rarity=fruit[1], image=shot)
            else:
                log("  couldn't read which fruit came out")
                self.debug_shot("chest_unknown_fruit")
            self.nap(1.5)
            opened += 1
            self.stats["chests_opened"] += 1
            log(f"  opened chest {k + 1}/{n}")
            tap(self.cfg["chest_key"])   # put the chest away so the next tap re-equips it cleanly
            self.nap(0.4)
        return opened

    def item_price(self):
        """Price of what shop runs buy: chests from the settings, rerolls from the last time it was read
        in the shop (or the setting, or 25 = Race Reroll x5)."""
        if self.cfg.get("shop_buy") == "rerolls":
            return self.cfg.get("reroll_price") or getattr(self, "seen_reroll_price", None) or 25
        return self.cfg["chest_price"]

    def find_shop_item(self, name):
        """Screen position of an item's name in the open shop (scrolls the list to find it)."""
        item = self.text.locate_text(name)
        if not item:
            self.shop_scroll(1)                    # from the top, one screen at a time
            for attempt in range(8):
                item = self.text.locate_text(name)
                if item or not self.shop_scroll(-1):
                    break
        return item

    def shop_price(self, name):
        """The price on an item's card (the number at its bottom right), or None."""
        box = self.text.locate_box([name])
        if not box and self.find_shop_item(name):
            box = self.text.locate_box([name])
        if not box:
            return None
        x0, y0, x1, y1 = box[4]
        h = y1 - y0
        m = self.text.mon
        W, H = m["width"], m["height"]
        ax0, ay0 = max(m["left"], x0 - 4 * h), y0
        area = [(ax0 - m["left"]) / W, max(0, (ay0 - m["top"]) / H),
                min(1, (x1 + 8 * h - m["left"]) / W), min(1, (y0 + 8 * h - m["top"]) / H)]
        from text_reader import find_text_boxes
        nums = []
        for text, conf, (bx0, by0, bx1, by1) in find_text_boxes(self.text.grab_area(area), 1.5):
            t = re.sub(r"[^0-9]", "", text.split()[0]) if text.split() else ""
            if t and int(t) > 0 and re.fullmatch(r"[\d,.]+\s*\S{0,4}", text.strip()):
                # the price's own card: the closest number under the name (then the closest sideways)
                nums.append((by0, abs(ax0 + bx1 - x1), int(t)))
        if not nums:
            return None
        top = min(n[0] for n in nums)
        row = [n for n in nums if n[0] - top < 1.5 * h]
        return min(row, key=lambda n: n[1])[2]

    def buy_one(self):
        if self.btn:   # same shop, same layout: reuse the positions found the first time (much faster)
            left_click_at(*self.btn[0])
            self.nap(0.7)
            left_click_at(*self.btn[1])
            self.nap(0.8)
            return True
        item = self.find_shop_item(self.buy_item)
        if not item:
            log(f"  couldn't find '{self.buy_item}' in the shop")
            self.debug_shot("shop_no_item")
            return False
        left_click_at(item[0], item[1])
        self.nap(0.7)
        ok = None
        t0 = time.time()
        while self.running and time.time() - t0 < 3.0:
            ok = self.text.locate_text("Accept")
            if ok:
                break
            self.nap(0.3)
        if not ok:
            log("  no Accept button after clicking the item")
            self.debug_shot("shop_no_accept")
            return False
        left_click_at(ok[0], ok[1])
        self.btn = ((item[0], item[1]), (ok[0], ok[1]))
        self.nap(0.8)
        return True

    def lap(self):
        if self.resume_plan:                       # "Continue": pick the lap up where it was stopped
            start_i, start_at = self.resume_plan
            self.resume_plan, self.resume = None, None
            t_lap = time.time()
            where = (f"point {start_i}" + (f", {start_at:.1f}s into that walk" if start_at else "")
                     if start_i < len(self.points) else "the death route")
            log(f"=== Lap {self.lap_no} (continued from {where}) ===")
            self.run_route(start_i, start_at, t_lap)
            return
        self.resume = None
        self.lap_no += 1
        self.knocked = 0
        t_lap = time.time()
        log(f"=== Lap {self.lap_no} ===")
        self.wait_focus()
        vis, share = self.hud_visible()
        if not vis and self.cfg.get("hud_set") and "hud_area" not in self.bad_boxes:
            if find_hud_area(self.text):              # the buttons ARE readable: your box is off
                self.drop_box("hud_area", "HUD buttons")
                vis, share = self.hud_visible()
        if not vis:
            log(f"HUD not visible (button colour {share:.3f}) - dead / respawning? "
                f"If you're clearly alive, run Settings > Diagnostics.")
            if self.wait_hud(True, 60) is None:
                log("HUD never showed up. Stopping.")
                self.event("stuck", lap=self.lap_no)
                self.running = False
                return
            self.nap(self.cfg["after_respawn"])
            self.alive_at = time.time()
        for step in self.cfg["pre_lap"]:
            if not self.running:
                return
            self.do_step(step)
        self.ensure_window()
        if self.to_open:
            if self.has_hotbar() and not self.ensure_chest():
                log("  no Rare Fruit Chest found to open - skipping the opening")
                self.to_open = 0
            else:
                self.to_open -= self.open_chests(self.to_open) or self.to_open   # don't retry forever
            if self.cfg["store_fruits"]:
                self.store_fruits()
        if self.pending_equip and self.cfg["manage_bucket"]:
            name, self.pending_equip = self.pending_equip, None
            log(f"Equipping the {name} you just bought")
            if self.equip_item(name):
                self.remember_bucket(name)
                self.nap(0.8)
                self.place_bucket([name])
            else:
                log(f"  couldn't equip the {name} - will try the normal bucket check")
        got = self.hold_bucket()                 # after a respawn nothing is equipped
        if not got and self.cfg["manage_bucket"]:
            state = self.fix_bucket()
            if state == "ok":
                got = self.hold_bucket()
            if not got and state == "unknown":
                log("Couldn't check the inventory properly - not buying anything, doing the lap anyway.")
            elif not got:
                if self.cfg["auto_upgrade"] and self.bag_tries < 2:
                    self.bag_tries += 1
                    log(f"No candy bucket at all -> shop run for the Pumpkin Bag (try {self.bag_tries}/2)")
                    self.candy_max = None
                    if self.shop_run(mode="upgrade"):
                        return
                elif self.cfg["auto_upgrade"]:
                    log("Still no candy bucket after 2 tries (5000 peli?). Stopping.")
                    self.event("stuck", lap=self.lap_no)
                    self.running = False
                    return
                else:
                    log("No candy bucket found (turn on Auto-upgrade to buy one). Knocking won't work.")
        if got:
            self.candies, self.candy_max = got
            log(f"Candies: {got[0]}/{got[1]}")
            self.event("candies", candies=got[0], max=got[1])
            have = self.cfg.get("bucket")
            if not have:
                have = {v: k for k, v in BUCKET_CAP.items()}.get(got[1])
                self.remember_bucket(have)
            if (self.cfg["auto_upgrade"] and have != "Candy Corn Basket" and got[0] >= got[1]
                    and str(got[1]) in self.cfg["upgrade_items"]):
                if self.shop_run(mode="upgrade"):
                    log(f"Lap {self.lap_no} was an upgrade run")
                    return
            elif got[0] >= min(self.cfg["shop_at"], got[1]) and got[1] >= 500 \
                    and got[0] >= self.item_price() and self.shop_run():
                log(f"Lap {self.lap_no} was a shop run")
                return
        else:
            log("Couldn't read the candy counter (is a candy bucket in its slot?)")
        self.spawn_score, self.spawn_gray = self.check_score(0)
        if self.spawn_score < self.cfg["spawn_min"]:
            log(f"(spawn view match {self.spawn_score:.2f}, low - fine if it's a new time of day)")
        left = self.cfg["cooldown"] - (time.time() - self.last_knock[self.first_door])
        if self.wait_first and left > 0:           # only after a bail-out (it asked to wait for door 1)
            log(f"Waiting {left:.0f}s for door 1's cooldown before starting")
            self.nap(left + 0.3)
        self.wait_first = False
        self.pos = 0
        self.suspect = 0
        self.run_route(1, 0.0, t_lap)

    def run_route(self, start_i, start_at, t_lap):
        """Doors from point start_i (start_at s into its walk), then the death route.
        If stopped on the way, remembers where (self.resume) so it can be continued."""
        n = len(self.points)
        for i in range(start_i, n):
            if not self.running:
                break
            res = self.visit(i, start_at if i == start_i else 0.0)
            if not self.running:
                if res == "stopped" and self.cut is not None:
                    self.resume = (i, self.cut)        # stopped mid-walk
                else:
                    self.resume = (i + 1, 0.0)         # stopped at / after the door
                log(f"(stopped at point {self.resume[0]}{f', {self.resume[1]:.1f}s into the walk' if self.resume[1] else ''}"
                    f" - Start offers to continue from here)")
                return
            if res == "lost":
                self.bail(i)
                self.finish_lap(t_lap, bailed=True)
                return
        if not self.running:
            self.resume = (start_i, start_at)
            return
        self.resume = None
        log(f"Lap {self.lap_no}: {self.knocked}/{len(self.door_no)} knocked, death route")
        self.die(self.death)
        self.finish_lap(t_lap)

    def finish_lap(self, t_lap, bailed=False):
        log(f"Lap {self.lap_no} {'cut short' if bailed else 'done'} in {time.time() - t_lap:.0f}s")
        self.stats["laps"] += 1
        self.stats["knocks"] += self.knocked
        self.event("lap", lap=self.lap_no, knocked=self.knocked, doors=len(self.door_no),
                   seconds=round(time.time() - t_lap), bailed=bailed)

    def test(self, what, n):
        log(f"Test '{what}' x{n}: focus Roblox and press {self.cfg['toggle_key'].upper()} to start "
            f"(respawned, camera untouched, at the spawn point).")
        keyboard.add_hotkey(self.cfg["toggle_key"], self.toggle)
        while not self.running:
            time.sleep(0.1)
        try:
            self.wait_focus()
            if what == "shop":
                self.shop_run(n)
                if self.to_open:
                    log(f"(after the respawn the next lap would open {self.to_open} chest(s))")
            else:
                self.open_chests(n)
        finally:
            release_all()
        log("Test done.")

    def loop(self):
        keyboard.add_hotkey(self.cfg["toggle_key"], self.toggle)
        log(f"{self.cfg['toggle_key'].upper()} = start/stop. Ctrl+C here to quit.")
        try:
            while True:
                if self.running:
                    self.lap()
                else:
                    time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            release_all()


# ----------------------------------------------------------------------------- diagnostics
class _Probe:
    """Just enough of a Bot to run its detectors (icons, slots) without a recorded route."""
    find_icons = Bot.find_icons
    _match_icons = Bot._match_icons
    find_bucket = Bot.find_bucket
    slot_box = Bot.slot_box
    hotbar = Bot.hotbar
    fruit_icons = Bot.fruit_icons
    _auto = Bot._auto
    hud = Bot.hud
    hud_area = Bot.hud_area
    candy_area = Bot.candy_area
    own_box_ok = Bot.own_box_ok
    drop_box = Bot.drop_box
    read_candies = Bot.read_candies
    message_area = Bot.message_area

    def __init__(self, cfg):
        self.cfg = cfg
        self.text = TextReader()
        self.icon_scales = {}
        self._slots, self._slots_good = None, None
        self.auto, self.auto_fail = {}, {}
        self.bad_boxes = set()
        self.icon_scale = None
        self.bucket_icons = {n: cv2.imread(resource(f), cv2.IMREAD_GRAYSCALE) for n, f in BUCKET_ICONS.items()}
        self.chest_icons = {"Rare Fruit Chest": cv2.imread(resource("chest_icon.png"), cv2.IMREAD_GRAYSCALE)}
        self.icon = cv2.imread(resource("fruit_icon.png"), cv2.IMREAD_GRAYSCALE)


def diagnostics():
    """Run every detector on what's on screen now. Returns (report lines, annotated screenshot)."""
    from text_reader import find_text
    cfg = load_cfg()
    kc = load_knock_cfg()
    probe = _Probe(cfg)
    shot = probe.text.grab_area([0, 0, 1, 1])
    H, W = shot.shape[:2]
    out = shot.copy()
    lines = [f"Looking at: {screen.describe()}  (mode: {cfg.get('capture')})"]

    def box(area, color, name):
        x0, y0, x1, y1 = area
        p1, p2 = (int(x0 * W), int(y0 * H)), (int(x1 * W), int(y1 * H))
        cv2.rectangle(out, p1, p2, color, 2)
        cv2.putText(out, name, (p1[0], max(14, p1[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    def ok(flag, text):
        lines.append(("OK   " if flag else "FIX  ") + text)

    # knock prompt
    if knock_template_source():
        try:
            pr = Prompt(kc)
            seen = pr.visible()
            lines.append(f"     Knock prompt template: {knock_template_source()}. Prompt on screen now: "
                         + (f"yes ({pr.best.name})" if seen else "no")
                         + f" (score {pr.last_score:.2f}, needs {pr.threshold:.2f})")
            if pr.region:
                box(pr.region, (0, 140, 255), "knock search region")
        except Exception as e:
            ok(False, f"Knock prompt: {e}")
    else:
        ok(False, "Knock prompt template missing (Setup > 1)")
    # candy counter (found by reading it; the bucket must be held)
    got = probe.read_candies(tries=1)
    area = probe.candy_area(find=False)
    src = "your box" if probe.own_box_ok("candy_area") else "found automatically"
    if area:
        box(area, (0, 220, 0), "candy counter")
    if got:
        ok(True, f"Candy counter ({src}) reads {got[0]}/{got[1]}")
    else:
        ok(False, "Candy counter not found - hold your candy bucket (so '123/500 Candies' shows) and run "
                  "Diagnostics again")
    # HUD
    h = probe.hud()
    if h:
        box(h[0], (255, 200, 0), "HUD buttons")
        share = hud_share(probe.text.grab_area(h[0]))
        src = "your box" if cfg.get("hud_set") and probe.own_box_ok("hud_area") else "found automatically"
        ok(share >= h[1], f"HUD buttons ({src}): colour {share:.3f} (alive above {h[1]:.3f})")
    else:
        ok(False, "HUD buttons (Menu / Backpack / Party / Daily Quests) not found - be alive with nothing "
                  "covering them. Without them death is detected with a fixed wait")
    # message area
    area = probe.message_area()
    box(area, (255, 0, 255), "message area")
    txt = [t for t, c in find_text(probe.text.grab_area(area))]
    lines.append(f"     Message area ({'your box' if probe.own_box_ok('banner_area', legacy=False) else 'automatic'}) text now: "
                 f"{txt or 'nothing (fine if no message is up)'}")
    # hotbar
    got = probe.hotbar(fresh=True)
    if got:
        (fx, fy, sw, sh, step), n = got
        auto = probe._slots is not None
        ok(True, f"Hotbar: {n} slot(s) " + ("found automatically" if auto else "from the box you drew (Setup)"))
        for k in [str(i) for i in range(1, 10)] + ["0"]:
            c, a = probe.slot_box(k)
            m = probe.text.mon
            cv2.circle(out, (c[0] - m["left"], c[1] - m["top"]), 4, (255, 255, 0), -1)
            cv2.putText(out, k, (c[0] - m["left"] - 4, c[1] - m["top"] - int(sh * H * 0.7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
        _, a_b = probe.slot_box(cfg["basket_key"])
        _, a_c = probe.slot_box(cfg["chest_key"])
        box(a_b, (0, 200, 255), f"bucket slot {cfg['basket_key']}")
        box(a_c, (255, 120, 0), f"chest slot {cfg['chest_key']}")
        b = probe.find_bucket(a_b)
        ok(bool(b), f"Slot {cfg['basket_key']}: {b[0] if b else 'no candy bucket icon found'}")
        c = probe.find_icons(a_c, probe.chest_icons, "chest")
        lines.append(f"     Slot {cfg['chest_key']}: {'Rare Fruit Chest' if c else 'no chest (fine if you have none)'}")
        lines.append(f"     '?' fruits in the hotbar: {len(probe.fruit_icons(cfg['hotbar_area']))}")
    else:
        ok(False, "Hotbar not found (dark scene?) - box it in Setup > Screen areas > Hotbar slots")
    # routes
    ok(os.path.exists(ROUTE_JSON), "Door route recorded" if os.path.exists(ROUTE_JSON) else "Door route missing (Setup > 3)")
    ok(os.path.exists(SHOP_JSON), "Shop route recorded" if os.path.exists(SHOP_JSON) else "Shop route missing (Setup > 4)")
    os.makedirs(os.path.join(BASE, "debug"), exist_ok=True)
    cv2.imwrite(os.path.join(BASE, "debug", "diagnostics.png"), out)
    return lines, out


def support_bundle(log_lines=None):
    """Zip everything needed to help someone (no webhook) next to the data. Returns the zip path."""
    import platform
    import zipfile
    path = os.path.join(BASE, f"support_{time.strftime('%Y%m%d_%H%M%S')}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        cfg = load_cfg()
        cfg["webhook_url"] = "(removed)" if cfg.get("webhook_url") else ""
        z.writestr("bot_config.json", json.dumps(cfg, indent=2))
        for f in ("knock_config.json", "knock_template.json", "knock_template.png", "log.txt"):
            if os.path.exists(os.path.join(BASE, f)):
                z.write(os.path.join(BASE, f), f)
        for f in ("route.json", "shop.json"):
            if os.path.exists(os.path.join(ROUTE_DIR, f)):
                z.write(os.path.join(ROUTE_DIR, f), "wasd_route/" + f)
        dbg = os.path.join(BASE, "debug")
        if os.path.isdir(dbg):
            for f in sorted(os.listdir(dbg))[-25:]:
                z.write(os.path.join(dbg, f), "debug/" + f)
        z.writestr("system.txt", f"version {VERSION}\nwindows {platform.platform()}\npython {platform.python_version()}\n"
                                 f"looking at: {screen.describe()}\n")
    return path


def check():
    cfg = load_cfg()
    with open(ROUTE_JSON) as f:
        data = json.load(f)
    cfg["work_h"] = data["work_h"]
    view = View(cfg, load_knock_cfg()["monitor"])
    tmpls = [view.template(cv2.imread(os.path.join(ROUTE_DIR, p["img"]), cv2.IMREAD_GRAYSCALE))
             for p in data["points"]]
    log("Best matching recorded points from where you stand (Ctrl+C to quit).")
    try:
        while True:
            frame = view.normalize(view.grab_gray())
            rows = []
            for i, t in enumerate(tmpls):
                (dx, dy), s = view.locate_in(frame, t, cfg["search_radius"])
                rows.append((s, i, dx, dy))
            rows.sort(reverse=True)
            print("  ".join(f"pt{i}:{s:.2f}@({dx:+.2f},{dy:+.2f})" for s, i, dx, dy in rows[:4]), flush=True)
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass


def record_shop(should_stop=None):
    """Spawn -> walk to the witch (shop pops up) -> F2 -> walk into the water -> F3."""
    state = {"stage": 0, "walk": [], "done": False}
    cur, down = [], {}

    def on_key(e):
        k = SCAN_TO_KEY.get(e.scan_code)
        if k is None or state["stage"] == 0:
            return
        now = time.time()
        if e.event_type == "down":
            if k in down:
                return
            down[k] = now
            cur.append((now, k, "down"))
        elif k in down:
            del down[k]
            cur.append((now, k, "up"))

    def take():
        now = time.time()
        for k in list(down):
            cur.append((now, k, "up"))
        down.clear()
        if not cur:
            return []
        t0 = cur[0][0]
        out = [[round(t - t0, 3), k, ty] for t, k, ty in cur]
        cur.clear()
        return out

    def f6():
        if state["stage"] == 0:
            state["stage"] = 1
            cur.clear()
            log("Recording the walk to the witch. Press F2 when the shop has popped up.")

    def f2():
        if state["stage"] == 1:
            state["walk"] = take()
            if not state["walk"]:
                log("No keys seen yet, walk to the witch first.")
                return
            state["stage"] = 2
            log(f"Walk to shop saved ({state['walk'][-1][0]:.1f}s). Now walk into the water, then press F3.")

    def f3():
        if state["stage"] == 2:
            death = take()
            if not death:
                log("No keys seen, walk into the water first.")
                return
            os.makedirs(ROUTE_DIR, exist_ok=True)
            with open(SHOP_JSON, "w") as f:
                json.dump({"walk": state["walk"], "death": death,
                           "auto_run": bool(load_cfg().get("auto_run"))}, f)
            log(f"Shop route saved (walk {state['walk'][-1][0]:.1f}s, to water {death[-1][0]:.1f}s).")
            state["done"] = True

    handles = [keyboard.hook(on_key)]
    hot = [keyboard.add_hotkey("f6", f6), keyboard.add_hotkey("f2", f2), keyboard.add_hotkey("f3", f3)]
    log("Respawn (don't touch the camera), equip the basket, stand on the spawn point and press F6.")
    while not state["done"]:
        if should_stop and should_stop():
            log("Recording cancelled, nothing saved.")
            break
        time.sleep(0.1)
    release_hooks(handles, hot)
    return state["done"]


def areas():
    """Mark the candy counter and the spawn-banner strip on a screenshot, then test reading them."""
    cfg = load_cfg()
    kc = load_knock_cfg()
    tr = TextReader(kc["monitor"])
    print("Switch to Roblox. Screenshot in 5 seconds...")
    time.sleep(5)
    full = tr.grab_area([0, 0, 1, 1])
    H, W = full.shape[:2]
    view_s = min(1.0, 1280 / W)
    view = cv2.resize(full, None, fx=view_s, fy=view_s, interpolation=cv2.INTER_AREA)

    def pick(title):
        print(f"{title}: drag a box, ENTER to confirm, C to skip.")
        x, y, w, h = cv2.selectROI(title, view, showCrosshair=False)
        cv2.destroyAllWindows()
        if w == 0 or h == 0:
            return None
        return [round(x / view_s / W, 4), round(y / view_s / H, 4),
                round((x + w) / view_s / W, 4), round((y + h) / view_s / H, 4)]

    a = pick("CANDY COUNTER (the '282/500 Candies' text)")
    if a:
        cfg["candy_area"] = a
        print("  reads:", tr.read_area(a), "->", tr.candies(a))
    b = pick("SPAWN BANNER strip (top center, make it wide: names change length)")
    if b:
        cfg["banner_area"] = b
        print("  finds:", tr.find_in_area(b))
    with open(BOT_CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print("Saved to bot_config.json")


def main():
    try:
        _main()
    except BotError as e:
        log(str(e))
        sys.exit(1)


def _main():
    mode = sys.argv[1] if len(sys.argv) > 1 else input("record, record_shop, run, check or areas? ").strip().lower()
    if mode == "record":
        record()
    elif mode == "run":
        Bot().loop()
    elif mode == "areas":
        areas()
    elif mode == "record_shop":
        record_shop()
    elif mode in ("test_shop", "test_open"):
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        Bot().test(mode[5:], n)
    elif mode == "check":
        if not os.path.exists(ROUTE_JSON):
            log("No route yet. Run: python knock_bot.py record")
            sys.exit(1)
        check()
    else:
        print("Use: python knock_bot.py record | run | check")


if __name__ == "__main__":
    main()