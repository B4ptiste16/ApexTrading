# APEX Trading Platform — Project Context

> One-stop briefing for any agent picking up this codebase. Skim this top-to-bottom and you'll have everything an established session has, except for the back-and-forth.

---

## 1. What this is

APEX is a **Windows desktop trading app** (PyQt6, packaged with PyInstaller) that runs **AI-driven trading bots** against the **Alpaca paper-trading API**. Three built-in bots — `LONG`, `SHORT`, `DAY` — use **Anthropic Claude (Sonnet / Haiku / Opus)** with Vision to score and rank candidates. A **FastAPI server** running on **Oracle Cloud** provides auth, encrypted broker-credential storage, a public bot marketplace, a phone-accessible web dashboard, and remote bot execution (bots can run on the cloud 24/7 instead of the user's laptop).

The user (Baptiste, GitHub `B4ptiste16`) is the sole developer and primary user. Single-account, personal project for now, but architecturally multi-tenant.

---

## 2. Repository

| | |
|---|---|
| GitHub | https://github.com/B4ptiste16/ApexTrading |
| Default branch | `main` |
| Local clone | `C:\Users\bapti\Documents\Trade app` (Windows) |
| `git config user.email` | `B4ptiste16@users.noreply.github.com` |
| `git config user.name` | `B4ptiste16` |
| Auth | GitHub CLI (`gh`) is signed in as `B4ptiste16` |
| `gh.exe` | `C:\Program Files\GitHub CLI\gh.exe` (sometimes not on `PATH` in fresh terminals — call by absolute path) |

### Pushing & releasing

```powershell
cd "C:\Users\bapti\Documents\Trade app"
git add <files>
git commit -m "..."
git push origin main

# Release with installer attached
$gh = "C:\Program Files\GitHub CLI\gh.exe"
& $gh release create "v1.1.X" "installer\APEX_Setup.exe" --title "..." --notes "..."
```

---

## 3. Local paths

| | |
|---|---|
| Project root | `C:\Users\bapti\Documents\Trade app` |
| User's APEX data dir (frozen) | `%LocalAppData%\APEX Trading Platform` |
| Sub-files there | `.env`, `apex_auth.json`, `apex_server.json`, `apex_settings.json`, `daybot_state.json`, `longv2_state.json`, `shortv2_state.json`, `*_trade_log.jsonl`, `longv2_charts/`, etc. |
| Build artifacts | `dist/APEX/` (PyInstaller output), `installer/APEX_Setup.exe` (Inno) |
| Oracle SSH key | `C:\Users\bapti\Documents\oracle server\ssh-key-2026-05-20.key` |

`.env` keys (the user manages these via Tools → API Keys in the app):
`ANTHROPIC_API_KEY`, `ALPACA_API_KEY_LONG`, `ALPACA_SECRET_KEY_LONG`, `ALPACA_API_KEY_SHORT`, `ALPACA_SECRET_KEY_SHORT`, `ALPACA_API_KEY_DAY`, `ALPACA_SECRET_KEY_DAY`.

---

## 4. Oracle Cloud deployment

| | |
|---|---|
| Provider | Oracle Cloud Infrastructure (Always-Free trial credits, AMD `VM.Standard.E5.Flex`) |
| Public IP | `145.241.170.165` (static) |
| SSH user | `opc` |
| SSH | `ssh -i "C:\Users\bapti\Documents\oracle server\ssh-key-2026-05-20.key" -o StrictHostKeyChecking=no opc@145.241.170.165` |
| OS | Oracle Linux 9, SELinux **enforcing** (important — every new binary in `/usr/local/bin` or files in `/opt/server` need `sudo chcon -t bin_t` or `sudo restorecon -Rv`) |
| Firewall | `firewalld` — already open: 22 (SSH), 80, 443, 8000 |
| Oracle Cloud Security List | Same ports open in the VCN's Default Security List (manual step in the web console) |

### What runs there

| Service | Path | Port | systemd unit |
|---|---|---|---|
| APEX auth + bot-runner API | `/opt/server/` (Python, FastAPI) | `127.0.0.1:8000` | `apex_server.service` |
| Caddy reverse proxy | `/usr/local/bin/caddy` (custom build with `caddy-dns/duckdns` plugin) | `80`, `443` | `caddy.service` |
| Bot subprocesses (per user, per bot) | `/opt/apex_bots/` ← bot `.py` files; spawned as `python -u -c "import M; M.main()"` | none | none (managed by `bot_runner.py` via `subprocess.Popen` + `start_new_session=True`) |
| Per-user state dirs | `/opt/apex_users/user_<id>/` | — | — |
| Encrypted creds + bot library + scheduled-bot list | `~/apex_data/apex_server.db` (SQLite, owned by `apex`) | — | — |

### Service users

| | |
|---|---|
| `opc` | Login/SSH user, has `sudo` without password |
| `apex` | System user that runs the auth server + spawns bots. Home dir `/home/apex`; data in `/home/apex/apex_data` (owned by `apex:apex`). |

### Critical install gotchas (already resolved, kept for reference)

* SELinux refuses to let systemd execute files from `/home/<user>/`. **All bot/server binaries live in `/opt/`**.
* When moving files from `/home/...` to `/opt/...`, they keep the `user_home_t` label; always run `sudo restorecon -Rv <path>` afterwards.
* The package layout expects `/opt/server/` to be importable as `server.<module>` — `WorkingDirectory=/opt` + `PYTHONPATH=/opt` in the systemd unit.
* `taskkill /F /IM APEX.exe /T` from the Windows updater would target its own grandparent process tree → "cannot terminate itself". Use `/F /IM APEX.exe` WITHOUT `/T`.
* On Python 3.11 (server's venv): **no backslash escapes inside f-string expressions**. Build complex HTML strings outside the f-string and interpolate.

### Server deploy command (run from Windows PowerShell)

```powershell
ssh -i "C:\Users\bapti\Documents\oracle server\ssh-key-2026-05-20.key" -o StrictHostKeyChecking=no opc@145.241.170.165 `
  "cd ~/apex_repo && git pull && sudo cp ~/apex_repo/server/*.py /opt/server/ && sudo chown apex:apex /opt/server/*.py && sudo restorecon -Rv /opt/server >/dev/null && sudo systemctl restart apex_server && sleep 2 && sudo systemctl is-active apex_server && curl -s http://localhost:8000/health"
```

---

## 5. Public URLs

| URL | Notes |
|---|---|
| `http://145.241.170.165:8000/` | Direct IP, raw port — works |
| `http://apexbaptou.duckdns.org/` | DuckDNS → reverse-proxied by Caddy to `:8000` |
| `https://apexbaptou.duckdns.org/` | **Currently broken** — Let's Encrypt validators get SERVFAIL on DuckDNS nameservers. Caddy is rebuilt with `caddy-dns/duckdns` plugin for DNS-01 challenge; still hits DuckDNS SERVFAIL. Caddyfile is currently set to HTTP-only as a workaround. Fix path: buy a real domain at Cloudflare (~$10/yr), point its A record at `145.241.170.165`, update Caddyfile. |
| Landing page | `/` |
| Phone dashboard | `/web/dashboard?tab=overview\|bots\|universe\|tools` |
| Login | `/web/login` |
| Signup | `/web/signup` |
| API: signup / login / me | `POST /auth/signup`, `POST /auth/login`, `GET /auth/me` |
| API: credentials | `PUT/GET/DELETE /credentials` (bearer auth) |
| API: schedule | `GET/PUT /schedule` (bearer auth) |
| API: bot lifecycle | `POST /bots/{side}/start`, `POST /bots/{side}/stop`, `GET /bots/{side}/status`, `GET /bots/{side}/logs`, `GET /bots/running` |
| API: marketplace | `GET /bots`, `GET /bots/{slug}`, `GET /bots/{slug}/download`, `POST /bots`, `DELETE /bots/{slug}` |
| Web JSON endpoints (cookie-auth) | `GET /web/api/status`, `POST /web/api/bots/{side}/start|stop|liquidate` |

DuckDNS:
* Subdomain: `apexbaptou`
* Token: `9e2b505a-79ca-494a-a473-8443bebb6280` (also embedded in `/etc/caddy/Caddyfile` for DNS-01 plugin)
* Manual DNS update: `https://www.duckdns.org/update?domains=apexbaptou&token=<TOKEN>&ip=145.241.170.165`

---

## 6. Versions

Tag history on GitHub: `v1.0.x` (V7 origin), then steady minor bumps. Auto-update logic compares local `version.json` to `https://raw.githubusercontent.com/B4ptiste16/ApexTrading/main/version.json`.

The current latest published is `v1.1.13` (Phase D — cloud-run toggle + chart fix). After that, this `Vnext.txt` is the next batch.

If the user is reporting weird auto-update behavior, **the most likely cause is they're on a version EARLIER than v1.1.5 where the relauncher batch was broken**. They need to manually install at least v1.1.5 from a GitHub Release once; from v1.1.5 onward in-app updates work via a visible Inno wizard (no `/SILENT`, no `taskkill /T`).

---

## 7. File map — what each file does

### Bots (Python, runnable both inside the frozen exe and on Oracle)

| File | Bot |
|---|---|
| `longbot_v2.py` | LONG (momentum + mean-reversion portfolio, Claude Vision on charts) |
| `shortbot_v2.py` | SHORT (bear momentum) |
| `daybot.py` | DAY (single high-conviction intraday bracket order — entry + GTC TP + GTC SL) |
| `universe_manager.py` | Refreshes the candidate ticker lists |

### Core (shared by desktop GUI + bots)

| File | Role |
|---|---|
| `core/data.py` | Single source of truth for: Alpaca client construction, account/positions/orders/history fetchers, JSON-Lines log reading, snapshot derivation, settings load/save, cost estimation, bot-metrics aggregation. **`load_snapshots()` derives from the trade log because no bot writes a separate snapshots file.** |
| `core/paths.py` | `DATA_DIR` resolution. `APEX_DATA_DIR` env var wins, then `LOCALAPPDATA\APEX Trading Platform` (frozen), else source root (dev). |
| `core/charts.py` | Plotly chart factories (equity curves, combined history, P/L bars, allocation pie) |
| `core/schedule.py` | US-market clock + auto-update window helpers (overnight gating) |
| `core/updater.py` | In-app self-update: notify-only banner; visible Inno installer; relauncher batch via `taskkill /F /IM APEX.exe` (no `/T`) + `CREATE_NO_WINDOW` `cmd /c`. Writes step-by-step log to `%TEMP%\apex_update.log`. |
| `core/worker.py` | Generic `QThread` wrappers (`DataWorker`, `RefreshWorker`, `OverviewWorker`) |

### Desktop UI (PyQt6)

| File | Role |
|---|---|
| `main.py` | `ApexWindow` (the main window), tab management, drag-reorderable bot tabs, drag-and-drop bot import, tray, header (clock + market badge + UPDATE banner + QUIT), auto-update wiring, `MoreBotsTab` (manage / browse marketplace / publish) |
| `ui/overview.py` | `OverviewTab` (sortable per-bot blocks, Period combo, AI cost cards) **and** `ToolsTab` (API keys, AUTOMATION row of per-bot auto-schedule checks + Run-on-Oracle checks, ACCOUNT LINKING with Sync-to-server, broker conversion exports, UPDATES section with Check-Now) |
| `ui/bot_tab.py` | `BotTab` per built-in bot, with all the sections: account cards, closed-trades feed, signal panel, gauge, equity chart, trades table, P/L bars, RISK METRICS, API COST, POSITION MANAGEMENT |
| `ui/make_bot_tab.py` | `MakeBotTab` — pick Anthropic/OpenAI/OpenRouter, paste API key, write English description, generate `.py`, save locally or publish to marketplace |
| `ui/login.py` | `LoginWindow` (gradient background, login/signup card, Continue-as-Guest, Continue-with-Google stub, server-status dot, configurable server URL) |
| `ui/universe.py` | Universe tab (editable ticker lists) |
| `ui/widgets.py` | Shared widgets: `BotProcessWidget` (▶/■/↺ + log textarea, with cloud-execution path that calls `/bots/{side}/start` etc.), `ChartView`, `MetricCard`, `SectionHeader`, `ScrollContent`, `DataTable`, `ClosedTradesFeed`, `WheelGuard` |
| `ui/styles.py` | `COLORS` dict + `DARK_STYLESHEET` |

### Server (FastAPI, runs on Oracle)

| File | Role |
|---|---|
| `server/app.py` | FastAPI app, routes, lifespan, CORS |
| `server/auth.py` | bcrypt + JWT (HS256, 30-day expiry). Secret from `APEX_JWT_SECRET` env var or auto-generated and persisted in `~/apex_data/.jwt_secret` |
| `server/schemas.py` | Pydantic request/response models |
| `server/database.py` | SQLite users + helpers (stdlib `sqlite3`, no ORM) |
| `server/credentials.py` | Fernet-encrypted broker creds per user (key derived from `APEX_JWT_SECRET` via SHA-256). Also stores the per-user `_schedule` list |
| `server/bots.py` | Public bot marketplace SQLite table + filesystem store at `~/apex_data/marketplace/<slug>.py` |
| `server/web.py` | HTML pages: `landing_page`, `login_page`, `signup_page`, `dashboard_page(tab=...)`; user session via `apex_token` cookie |
| `server/bot_runner.py` | Spawns/tracks per-user bot processes. Uses `/opt/apex_venv/bin/python -u -c "import M; M.main()"`; per-user `APEX_DATA_DIR=/opt/apex_users/user_<id>/` |
| `server/scheduler.py` | Once-a-minute async loop in the FastAPI event loop. Polls US market clock (Alpaca's `get_clock()` via any user with linked keys; falls back to ET wall-clock). For every user, reconciles scheduled-bot list against running bots |
| `server/run.py` | Dev runner: `python server/run.py` |
| `server/requirements.txt` | fastapi, uvicorn[standard], bcrypt, PyJWT, pydantic, python-multipart, cryptography |

### Build / release

| File | Role |
|---|---|
| `build.bat` | One-shot PyInstaller `--onedir` build + Inno Setup compile → `installer\APEX_Setup.exe`. Bundles `version.json`, `BOT_SKELETON.md`, `assets/` |
| `release.bat` | Bump patch version → call `build.bat` → git push → `gh release create`. **Not safe for minor/major bumps** — when bumping minor, edit `version.json` manually and run `build.bat` + `gh release create` yourself |
| `installer.iss` | Inno Setup script. Per-user install to `%LocalAppData%\APEX Trading Platform\`, `PrivilegesRequired=lowest`, `[Run]` launches `APEX.exe` post-install with `Flags: nowait postinstall skipifsilent` |
| `requirements.txt` | App dependencies (PyQt6, pandas, numpy, alpaca-py, anthropic, yfinance, matplotlib, plotly, python-dotenv, requests) |
| `BOT_SKELETON.md` | Bundled at install time — appears in `_MEIPASS/BOT_SKELETON.md`; surfaced by Tools → "Open skeleton guide" and used as the system prompt by `ui/make_bot_tab.py` so AI-generated bots follow the APEX contract |

### Docs / planning

| File | Role |
|---|---|
| `V7.1+.txt` | User's V7.1+ wishlist (mostly shipped) |
| `Vnext.txt` | Current pending wishlist (this batch) |
| `PROJECT_CONTEXT.md` | **This file** |

---

## 8. Build commands

```powershell
cd "C:\Users\bapti\Documents\Trade app"

# Update version manually
notepad version.json     # set "version": "1.1.X" and notes

# Syntax-check changed files
python -m py_compile core/data.py ui/overview.py ui/widgets.py main.py    # add others as needed

# Full build (~5 min) — PyInstaller + Inno Setup
$env:APEX_NOPAUSE = "1"
.\build.bat 2>&1 | Tee-Object -FilePath build_log.txt
# OR run in background:
.\build.bat 2>&1 | Tee-Object -FilePath build_log.txt   # via Bash tool with run_in_background:true

# Verify
Get-Item installer\APEX_Setup.exe | Select-Object Length, LastWriteTime

# Publish
git add -A; git commit -m "..."; git push origin main
$gh = "C:\Program Files\GitHub CLI\gh.exe"
& $gh release create "v1.1.X" "installer\APEX_Setup.exe" --title "..." --notes "..."
```

Toolchain on the Windows laptop:
* Python 3.14.5 (note: bleeding-edge — most things work; if something doesn't, Python 3.12 LTS is the safe fallback)
* `python -m pip` works; standalone `pip` not on PATH
* Inno Setup 6 at `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`
* gh CLI at `C:\Program Files\GitHub CLI\gh.exe`

---

## 9. Where things stand (Vnext.txt items)

| # | Item | Status |
|---|---|---|
| 1 | Tooltip "?" icons next to metrics | not started |
| 2 | Chart spans close-to-close | not started (chart currently forward-fills NaN gaps which gives a flat overnight line — works for visualization but doesn't skip non-update hours yet) |
| 3 | Level right corner tabs with left tabs | not started |
| 4 | Per-stock target prices on long-bot chart | not started |
| 5 | "Update existing bot" in MAKE BOT | not started |
| 6 | Free AI option in MAKE BOT | not started |
| 7 | Phone bot detail page + liquidate confirmation | not started |
| 8 | Phone overview: period + portfolio % | not started |
| 9 | Phone status-bar color fix | not started |

---

## 10. Architectural decisions worth knowing

* **No ORM on server** — uses stdlib `sqlite3` because the schema is tiny and dependency-free is a feature on a small VM.
* **Bot subprocesses run as `apex` user** under the FastAPI server's process. They survive uvicorn restarts because `start_new_session=True` is set.
* **Encryption key derivation**: Fernet key = `base64.urlsafe_b64encode(sha256(APEX_JWT_SECRET))`. Same secret for JWT signing AND credential encryption. **If `APEX_JWT_SECRET` ever rotates, all stored credentials become unreadable** (we treat that as expected; clients re-upload).
* **One snapshot source of truth**: `load_snapshots(side)` derives from the bot's `*_trade_log.jsonl` file because no bot writes a dedicated snapshots file. Supports both schemas: LONG/SHORT have `portfolio_before/after.portfolio_value`; DAY has `portfolio.value`.
* **Updates are notify-only** — never auto-apply. The user sees a banner in the header. Click → release notes → click Install → visible Inno wizard. No SmartScreen surprises.
* **Auto-schedule** stays on the desktop's `_tick_schedule` for local bots; **cloud bots** are managed by `server/scheduler.py`'s once-a-minute reconciliation loop. The intersection of `cloud_bots ∩ auto_schedule_bots` is what gets pushed to the server's `_schedule` field.
* **Single-instance lock** on the desktop: `ctypes.windll.kernel32.CreateMutexW(None, False, "APEX_Trading_Platform_SingleInstance_v7")` at startup. Double-click on the exe → second instance exits silently.

---

## 11. Working with the user

Conversational style preferences observed across the build:
* Concise, table-based answers for plans.
* The user appreciates **before/after** explanations of bug fixes (what was broken / why / fix).
* The user types fast — small typos are common (e.g. "thq", "wqs"); treat them as the intended English word.
* Bilingual French/English context; comments and code should stay in English.
* The user has a Claude credit budget — **don't redo work or re-derive context that's in this file**.
* The user can SSH into Oracle themselves, but you can also SSH from your own PowerShell tool using the SSH key path above. Do it yourself when a server change is needed — saves a round-trip.

---

## 12. Useful diagnostic snippets

```bash
# Server health
ssh -i "C:\Users\bapti\Documents\oracle server\ssh-key-2026-05-20.key" opc@145.241.170.165 \
  "sudo systemctl status apex_server --no-pager | head -8; curl -s http://localhost:8000/health"

# Tail server log
ssh ... "sudo journalctl -u apex_server -n 50 --no-pager"

# Cert / Caddy status
ssh ... "sudo journalctl -u caddy -n 30 --no-pager"

# What's running on the user's account
$token = (Get-Content "$env:LocalAppData\APEX Trading Platform\apex_auth.json" | ConvertFrom-Json).token
curl.exe -H "Authorization: Bearer $token" http://145.241.170.165:8000/bots/running
```

---

## 13. Open issues / things that still bite occasionally

* **HTTPS on `apexbaptou.duckdns.org`** is currently disabled. DuckDNS's nameservers intermittently SERVFAIL Let's Encrypt's validators (both HTTP-01 and DNS-01 paths). Workaround: HTTP only. Real fix: buy a domain ($10/yr at Cloudflare Registrar), point A-record at `145.241.170.165`, update `/etc/caddy/Caddyfile`.
* The desktop **app freezes briefly** on bot-tab drag if you held a cloud-poll cycle. v1.1.13 mitigated it. If reports come back, the next defense is to push the cloud-status poll into a background thread instead of a `QTimer` on the main thread.
* `pandas_market_calendars` is **not yet a dependency** — needed if we want to implement Vnext item 2 (skip non-market hours by holiday calendar). Add to `requirements.txt` and bundle it via PyInstaller (`--collect-all pandas_market_calendars` in `build.bat`).
* DAY bot's `rearm_orphaned_positions` (v1.1.10) only runs on bot startup. If a position becomes orphaned mid-session, it'll wait for the next bot run. Acceptable for now.

---

That's everything. A fresh agent reading this should be productive in the codebase within minutes — paths, IPs, conventions, gotchas, and the current to-do list are all here.
