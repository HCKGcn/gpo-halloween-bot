"""
prepare_seed.py - run by build_exe.bat. Copies your current setup (template, configs, routes)
into ./seed so it gets baked into the exe.  `python prepare_seed.py public` leaves out your
Discord webhook and your screen-area boxes (use that when giving the exe to someone else).
`defaults` bakes in the shared setup committed in ./defaults (knock template, routes) - the GitHub
release build. `empty` bakes in nothing.
"""
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["knock_template.png", "knock_template.json", "knock_config.json", "bot_config.json"]
DIRS = ["wasd_route"]
SEED = os.path.join(HERE, "seed")
public = "public" in sys.argv[1:]
empty = "empty" in sys.argv[1:]
defaults = "defaults" in sys.argv[1:]
SRC = os.path.join(HERE, "defaults") if defaults else HERE
if defaults:
    public = True
    if not os.path.isdir(SRC):
        empty = True
# boxes drawn on YOUR screen: wrong on anyone else's (they get the automatic ones instead)
PERSONAL = {"webhook_url": "", "candy_area": None, "banner_area": None, "hotbar_slots": None, "hud_set": False}

shutil.rmtree(SEED, ignore_errors=True)
os.makedirs(SEED)
if empty:                          # public release: no personal setup inside, everyone sets up their own
    with open(os.path.join(SEED, "README.txt"), "w") as f:
        f.write("Empty on purpose: this build carries no personal setup.\n")
    print("Empty seed (public release build).")
    sys.exit(0)
found = []
for n in FILES:
    src = os.path.join(SRC, n)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(SEED, n))
        found.append(n)
for n in DIRS:
    src = os.path.join(SRC, n)
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(SEED, n))
        found.append(n + "/ (" + str(len(os.listdir(src))) + " files)")

cfg_path = os.path.join(SEED, "bot_config.json")
if public and os.path.exists(cfg_path):
    with open(cfg_path) as f:
        cfg = json.load(f)
    for k, v in PERSONAL.items():
        if k in cfg:
            cfg[k] = v
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)

with open(os.path.join(SEED, "seed_id.txt"), "w") as f:
    f.write(time.strftime("%Y%m%d_%H%M%S"))

print("Baking into the exe:", ", ".join(found) if found else "nothing (fresh setup)")
if public:
    print("Public build: Discord webhook and screen-area boxes removed.")
