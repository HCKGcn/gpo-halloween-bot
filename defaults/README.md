# Built-in setup

Everything in this folder is baked into the release exe (`prepare_seed.py defaults`), so people don't have
to set anything up. Put here, from `%APPDATA%\GPOTrickBot` on a PC where the bot works:

| File | What |
|---|---|
| `knock_template.png`, `knock_template.json`, `knock_config.json` | optional: a knock prompt template. The app already has a built-in one (`knock_default.png`) that works at any size |
| `wasd_route/` | the door route (`route.json` + its snapshots) and the shop route (`shop.json`) |
| `bot_config.json` | optional: default settings |

The build always removes the Discord webhook and any screen boxes (candy counter, message area, HUD,
hotbar) from `bot_config.json`, so everyone gets the automatic ones.
