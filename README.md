# Bookkeeping Tracker

A reconciliation tracker for bookkeeping clients: bank and liability accounts by month, check registers, weekly payroll, and printable missing-items reports and client letters. All data lives in a single SQLite database file.

It runs two ways:

- **Server mode (recommended):** `python app.py` serves the tracker and stores the database on disk. Every change saves automatically, the server keeps rolling backups, and you can reach it from any browser you allow.
- **Single file:** open `index.html` directly in Chrome or Edge and pick a `.db` file. This needs no setup, but browsers restrict what pages opened from disk (and especially from network shares) can do.

## Features

- **Clients and accounts:** bank accounts (checking, savings, money market) and liability accounts (credit cards, lines of credit, loans), grouped separately. Each account has an optional General Ledger, statement closing day, open/closed months and notes.
- **Monthly tracking:** statement and GL status, reconciled flag, beginning/ending/GL balances with chain-break and out-of-balance checks, overdue statements, and a year-end note for cards that close mid-month.
- **Check registers:** multiple number series per account (e.g. handwritten 1000s, payroll 5000s), bulk entered/cleared/void, gaps, outstanding-as-of-month and stale checks.
  - Import from a QuickBooks Online transaction report (`Num`, `Description`, `Cleared` columns; reads "Check 1234" from the description when Num is blank).
  - Clear checks from a pasted or uploaded bank statement.
- **Payroll:** weekly runs, quarterly Form 941 / NYS-45 received and tied out, annual W-3.
- **Reports:** internal checklist, letter to client, or missing-items list for whoever contacts the client. Sections and options can be toggled, wording is editable and can be saved per client, and output prints cleanly in black and white.
- Undo (Ctrl+Z), CSV export/import for moving data, stable UUIDs on clients, accounts and check series.

## Quick start (your own computer)

Requires Python 3.9 or newer. No packages to install.

```bash
git clone https://github.com/ItsAloof/bookkeeeping-tracker.git
cd bookkeeeping-tracker
python app.py
```

Open <http://localhost:8000>. With no password set, the server only accepts connections from the same computer.

The first time it runs, the server downloads the SQLite engine (sql.js) into `data/vendor/` so the page doesn't depend on a CDN afterwards. If that download is blocked, the browser loads sql.js from a CDN instead.

### Bring over an existing database

Either upload it from the page (**File → Upload a .db to the server**), or before starting the server:

```bash
python app.py --import-db path/to/reconciliation-tracker.db
```

Older tracker files are upgraded automatically when first opened.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TRACKER_PASSWORD` | *(none)* | Password for the browser sign-in. **Without it, only this computer can connect.** |
| `TRACKER_USER` | `tracker` | Username for the sign-in. |
| `TRACKER_DATA_DIR` | `./data` | Where the database, backups and cached engine live. |
| `TRACKER_BACKUPS` | `60` | How many backups to keep. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address when run with `python app.py` (also `--host`, `--port`). |

To use it from other devices on your network:

```bash
# macOS / Linux
TRACKER_PASSWORD='choose-a-long-password' python app.py --host 0.0.0.0
# Windows PowerShell
$env:TRACKER_PASSWORD='choose-a-long-password'; python app.py --host 0.0.0.0
```

The sign-in is HTTP Basic auth, so outside your own network it must go over **HTTPS**. The hosting options below provide that.

## Hosting

The tracker stores client financial data, so check your firm's policy before putting it on a third-party host. Running it on a machine you control is usually the safest choice.

| Option | Cost | Notes |
|---|---|---|
| Your own computer | Free | `python app.py`. Data never leaves the machine. |
| Your computer + [Tailscale](https://tailscale.com/) | Free for personal, non-commercial use; paid plans for business | Private encrypted access from your phone or laptop without exposing anything to the internet. |
| [PythonAnywhere](https://www.pythonanywhere.com/) free account | Free | One web app, 512 MB disk, HTTPS on `yourname.pythonanywhere.com`. The free web app expires after a month unless you log in and extend it. |
| [Fly.io](https://fly.io/) with a volume | Roughly $2–3/month | Smallest machine plus a 1 GB volume. No free tier for new accounts. Uses the included `Dockerfile`. |
| Render free tier | Not suitable | Free web services have no persistent disk, so the database would be wiped on restart. |

Prices and free-tier limits change. Check each provider's current pricing page.

### PythonAnywhere (free)

1. Create a free account, open a **Bash console**, and clone the repo:
   ```bash
   git clone https://github.com/ItsAloof/bookkeeeping-tracker.git
   ```
2. **Web** tab → **Add a new web app** → **Manual configuration** → pick the newest Python.
3. Open the **WSGI configuration file** link and replace its contents with (set the password *before* the import):
   ```python
   import os, sys
   os.environ["TRACKER_PASSWORD"] = "choose-a-long-password"
   os.environ["TRACKER_DATA_DIR"] = "/home/YOURUSERNAME/tracker-data"
   sys.path.insert(0, "/home/YOURUSERNAME/bookkeeeping-tracker")
   from app import application
   ```
4. Turn on **Force HTTPS**, click **Reload**, and visit `https://YOURUSERNAME.pythonanywhere.com`.

### Fly.io (about $2–3/month)

```bash
fly launch --no-deploy                       # detects the Dockerfile; choose a region, no databases
fly volumes create tracker_data --size 1
fly secrets set TRACKER_PASSWORD='choose-a-long-password'
```

Add this to the generated `fly.toml`, make sure `internal_port = 8080`, then run `fly deploy`:

```toml
[mounts]
  source = "tracker_data"
  destination = "/data"
```

## Backups

Every save replaces `data/tracker.db` atomically, so a crash can't leave a half-written file. Before overwriting, the server copies the previous version into `data/backups/` (at most once every 30 minutes, and always before a full replace) and keeps the newest 60. To restore, stop the server and copy a backup over `data/tracker.db`, or upload it from the page.

You can also download the current database from the page (**File → Download a backup copy**) or from `/api/db?download=1`.

## How saving works

The page keeps the database in memory and sends the whole file to the server after each change (`PUT /api/db`). Each save carries the version it started from, and the server rejects it if the database changed in another tab or device, so two windows can't silently overwrite each other. If that happens, the save indicator turns red; click it to load the latest version.

| Endpoint | Purpose |
|---|---|
| `GET /` | The tracker page |
| `GET /api/info` | Whether a database exists, its version and size |
| `GET /api/db` | Download the database (`ETag` = version) |
| `PUT /api/db` | Save the database; requires `If-Match` with the loaded version, or `*` to replace |
| `GET /api/backups` | List backups |
| `GET /healthz` | Health check (no sign-in needed) |

## Using the data from Python

`data/tracker.db` is a standard SQLite file, readable with the built-in `sqlite3` module. Amounts are stored as integer cents; months are `0`–`11`.

| Table | Contents |
|---|---|
| `clients` | `uuid`, name, notes, archived |
| `accounts` | `uuid`, client, type (`checking`, `credit_card`, …), institution, last 4, has GL, closing day, opened/closed (`YYYY-MM`) |
| `periods` | per account, year and month: statement and GL status, reconciled, beginning/ending/GL balance, note |
| `check_series`, `checks` | check number series per account; each check's state (`entered` / `cleared` / `void`) and entered/cleared month |
| `payroll_setup`, `payroll_runs`, `payroll_quarters`, `payroll_years` | weekly pay dates and status, quarterly 941 / NYS-45, annual W-3 |
| `settings` | app settings, report templates and per-client report preferences |

```python
import sqlite3
con = sqlite3.connect("data/tracker.db")
for name, n in con.execute("""
    SELECT a.name, COUNT(*) FROM checks k
    JOIN check_series s ON s.id = k.series_id JOIN accounts a ON a.id = s.account_id
    WHERE k.state = 'entered' GROUP BY a.id"""):
    print(f"{name}: {n} outstanding checks")
```

Write to the database only while the server is stopped, or through the API, since the page holds its own copy in memory.

## Project layout

```
app.py            Python server (standard library only; WSGI app = app:application)
index.html        The tracker (single page; also works opened directly as a file)
Dockerfile        Container image for Fly.io or any Docker host
requirements.txt  Empty on purpose (no dependencies)
data/             Created at runtime: tracker.db, backups/, vendor/ (git-ignored)
```
