"""
notify.py - Discord webhook messages + the fruit-spawn watcher.

    send_discord(url, title, text, color=0xFF005F, image_png=None)   -> (ok, error)
    SpawnWatcher(reader, area, on_spawn).start() / .stop()
"""
import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid

import cv2
import numpy as np

from text_reader import parse_spawn

ACCENT = 0xFF005F
UA = "GPOTrickBot/1.0 (+https://discord.com)"   # Discord rejects requests without a User-Agent


def send_discord(url, title, text="", color=ACCENT, image_png=None, fields=None, mention=""):
    """Post an embed to a Discord webhook. image_png: bytes of a PNG to attach and show in the embed."""
    if not url or not url.startswith("http"):
        return False, "no webhook URL set"
    embed = {"title": title, "description": text, "color": color,
             "footer": {"text": "GPO Trick-or-Treat Bot"},
             "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if fields:
        embed["fields"] = [{"name": k, "value": str(v), "inline": True} for k, v in fields]
    payload = {"username": "GPO Bot", "content": mention or None, "embeds": [embed]}
    try:
        if image_png:
            embed["image"] = {"url": "attachment://shot.png"}
            boundary = uuid.uuid4().hex
            body = io.BytesIO()
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                       f"Content-Type: application/json\r\n\r\n".encode())
            body.write(json.dumps(payload).encode())
            body.write(f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
                       f"filename=\"shot.png\"\r\nContent-Type: image/png\r\n\r\n".encode())
            body.write(image_png)
            body.write(f"\r\n--{boundary}--\r\n".encode())
            data, ctype = body.getvalue(), f"multipart/form-data; boundary={boundary}"
        else:
            data, ctype = json.dumps(payload).encode(), "application/json"
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": ctype, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}"
    except Exception as e:
        return False, str(e)


def png_bytes(bgr):
    ok, buf = cv2.imencode(".png", bgr)
    return buf.tobytes() if ok else None


class SpawnWatcher(threading.Thread):
    """Watches the message area at the top of the screen (where 'A X has spawned at Y', 'New Item <...>'
    and fruit messages show up). Reads it only when the area changes (cheap pixel check)."""

    def __init__(self, reader_factory, area, on_spawn, interval=2.0, log=print,
                 on_item=None, on_fruit=None, known_items=None):
        super().__init__(daemon=True)
        self.reader_factory, self.area, self.on_spawn = reader_factory, area, on_spawn
        self.on_item, self.on_fruit, self.known_items = on_item, on_fruit, known_items or []
        self.reader = None
        self.interval, self.log = interval, log
        self.alive = True
        self.recent = {}          # key -> time, so one event = one message
        self.last_small = None
        self.last_read = 0.0

    def stop(self):
        self.alive = False

    def run(self):
        self.reader = self.reader_factory()   # screen capture objects must live in the thread using them
        while self.alive:
            try:
                self.tick()
            except Exception as e:
                self.log(f"(message watcher: {e})")
                time.sleep(5)
            time.sleep(self.interval)

    def fresh_key(self, key, window):
        now = time.time()
        if now - self.recent.get(key, 0) < window:
            return False
        self.recent[key] = now
        return True

    def tick(self):
        from screen import auto_message_area
        img = self.reader.grab_area(self.area or auto_message_area(self.reader.mon))
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (96, 24), interpolation=cv2.INTER_AREA)
        changed = self.last_small is None or float(np.mean(cv2.absdiff(small, self.last_small))) > 3.0
        self.last_small = small
        if not changed and time.time() - self.last_read < 20:
            return
        self.last_read = time.time()
        from text_reader import find_text, parse_item, fruit_lookup
        for text, conf in find_text(img):
            got = parse_spawn(text)
            if got:
                fruit, rarity = fruit_lookup(got[0])
                # one alert per fruit spawn, even if the place gets read two ways ("Shell's Town" / "Shell'stown")
                if self.on_spawn and self.fresh_key(("spawn", (fruit or got[0]).lower()), 600):
                    self.on_spawn(fruit or got[0], got[1], text, img, rarity)
                continue
            item = parse_item(text, self.known_items, strict=True)   # only the real event items
            if item and self.on_item and self.fresh_key(("item", item.lower()), 600):
                self.on_item(item, text, img)
