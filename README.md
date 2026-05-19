# APEX Trading Platform — Desktop App

A native Windows desktop application for monitoring and managing your
three AI trading bots (Long, Short, Day) built with PyQt6.

---

## INSTALL

```bash
pip install PyQt6 PyQt6-WebEngine plotly pandas numpy yfinance python-dotenv anthropic alpaca-py requests
```

---

## RUN

```bash
cd apex_app
python main.py
```

The app will:
- Open as a native desktop window
- Appear in your system tray (close button minimises to tray)
- Auto-refresh data every 20 seconds
- Check for updates on startup

---

## FOLDER STRUCTURE

Place the `apex_app/` folder inside your existing project:

```
Trade_bot/10/
├── apex_app/           ← this app
│   ├── main.py         ← entry point
│   ├── version.json    ← version tracking
│   ├── core/
│   │   ├── data.py     ← Alpaca + log data
│   │   ├── charts.py   ← Plotly chart generators
│   │   └── updater.py  ← GitHub auto-updater
│   └── ui/
│       ├── styles.py   ← Qt stylesheets
│       ├── widgets.py  ← reusable components
│       ├── bot_tab.py  ← per-bot tab
│       └── overview.py ← overview + tools tabs
│
├── longbot_v2.py
├── shortbot_v2.py
├── daybot.py
├── universe_manager.py
├── .env
└── ...
```

---

## BUILD AS .EXE (standalone Windows app)

```bash
pip install pyinstaller
cd apex_app
pyinstaller --onefile --windowed --name APEX main.py
```

The `.exe` will be in `apex_app/dist/APEX.exe`.
Double-click to run — no Python installation needed on that machine.

---

## AUTO-UPDATES

To enable automatic updates from GitHub:

1. Create a GitHub repository with your project files
2. Edit `core/updater.py` → set `GITHUB_REPO = "yourname/your-repo"`
3. Add `version.json` to your repo root
4. Push updates to GitHub
5. The app checks on every launch and shows an update button if a new version exists

Files that are NEVER overwritten by updates:
- `.env` (your API keys)
- `*_state.json` (bot state)
- `*_universe.txt` (your ticker lists)
- `*_watchlist.txt`
- All log files and chart images

---

## CLOUD ACCESS

**Local network** (any device on your WiFi):
- The app displays your local IP on the Tools tab
- Open `http://YOUR_IP:8050` on any phone/tablet/laptop

**Internet access from anywhere**:
```bash
pip install pyngrok
ngrok http 8050
```
Use the URL ngrok provides from any device, anywhere in the world.

---

## BOT CONTROLS

Each bot tab has:
- **▶ RUN BOT** — starts the bot script as a subprocess
- **■ STOP** — gracefully stops it
- **↺ RESTART** — stop + start
- **Live log** — shows bot output in real time
- **Replace Script** — drag or browse to a new .py file to hot-swap the bot

---

## POSITION MANAGEMENT

Each bot tab has a position table with a LIQUIDATE button.
Select any position from the dropdown and click LIQUIDATE to
call `close_position()` directly on Alpaca — instant execution.
Dust positions (value < $1) are flagged with ⚠.
