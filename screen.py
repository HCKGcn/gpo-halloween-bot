"""
screen.py - where the game is on screen.

Everything the bot looks at (areas, templates, snapshots) and everything it clicks is measured relative
to ONE rectangle:
  * mode "window"  (default): the inside of the Roblox window (no title bar / borders). Works with
                              Roblox fullscreen or windowed, on any monitor, at any size and position.
  * mode "monitor" (old setups): the whole monitor.
"""
import ctypes
import ctypes.wintypes as wt

import mss

MODE = "window"
MONITOR = 1

try:
    user32 = ctypes.windll.user32
except AttributeError:      # not on Windows (development / tests)
    user32 = None


def configure(mode=None, monitor=None):
    global MODE, MONITOR
    if mode in ("window", "monitor"):
        MODE = mode
    if monitor:
        MONITOR = int(monitor)


def monitor_rect(index=None):
    with mss.mss() as sct:
        mons = sct.monitors
        m = mons[min(max(index or MONITOR, 1), len(mons) - 1)]
        return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}


def roblox_hwnd():
    """The Roblox window: the focused one if Roblox is in front, else the first one found."""
    if not user32:
        return None
    buf = ctypes.create_unicode_buffer(256)
    fg = user32.GetForegroundWindow()
    user32.GetWindowTextW(fg, buf, 256)
    if buf.value == "Roblox":
        return fg
    return user32.FindWindowW(None, "Roblox") or None


def window_client_rect(hwnd):
    """The inside of a window (without title bar and borders), in screen pixels."""
    r = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return {"left": pt.x, "top": pt.y, "width": r.right - r.left, "height": r.bottom - r.top}


def capture_rect():
    """The rectangle everything is relative to (see the top of this file)."""
    if MODE == "window" and user32:
        h = roblox_hwnd()
        if h and not user32.IsIconic(h):
            rect = window_client_rect(h)
            if rect and rect["width"] >= 300 and rect["height"] >= 200:
                return rect
    return monitor_rect()


def describe():
    r = capture_rect()
    src = "Roblox window" if MODE == "window" and user32 and roblox_hwnd() else "monitor"
    return f"{src} {r['width']}x{r['height']} at ({r['left']}, {r['top']})"


def auto_message_area(mon):
    """Where game messages show up: top centre, from the compass down to well below the
    SAFE ZONE banner ("A X has spawned at Y", "New Item <...>", "You unboxed ..."). The game's UI
    scales with the window height, so the width is measured in heights."""
    half = min(0.5, 0.55 * mon["height"] / max(1, mon["width"]))
    return [round(0.5 - half, 4), 0.03, round(0.5 + half, 4), 0.36]
