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


def make_dpi_aware():
    """Work in real screen pixels on every monitor, whatever its Windows display scaling (100 %, 125 %,
    150 %...). Without this, a window on a scaled monitor - or a second monitor with a different scaling -
    reports shrunken coordinates and every screenshot lands in the wrong place. Call before any window."""
    if not user32:
        return
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):     # per-monitor v2 (Windows 10+)
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)                     # per-monitor (Windows 8.1+)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


make_dpi_aware()


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
    h = roblox_hwnd() if user32 else None
    if MODE == "window" and h:
        src = "Roblox window"
    elif MODE == "window":
        src = "monitor (no Roblox window found - is Roblox open and not minimized?)"
    else:
        src = "monitor"
    extra = ""
    try:
        if h:
            extra = f", display scaling {round(user32.GetDpiForWindow(h) / 96 * 100)}%"
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors[1:], 1):
                if m["left"] <= r["left"] < m["left"] + m["width"] and m["top"] <= r["top"] < m["top"] + m["height"]:
                    extra += f", on monitor {i} ({m['width']}x{m['height']})"
    except Exception:
        pass
    return f"{src} {r['width']}x{r['height']} at ({r['left']}, {r['top']}){extra}"


def auto_message_area(mon):
    """Where game messages show up: top centre, from the compass down to well below the
    SAFE ZONE banner ("A X has spawned at Y", "New Item <...>", "You unboxed ..."). The game's UI
    scales with the window height, so the width is measured in heights."""
    half = min(0.5, 0.55 * mon["height"] / max(1, mon["width"]))
    return [round(0.5 - half, 4), 0.03, round(0.5 + half, 4), 0.36]
