# ArchersHub Auto Switch

Python utility for logging in to ArchersHub, monitoring a course section, and automatically switching to a target section when a slot opens.

## Setup

```bash
poetry install
```

## Usage

```bash
poetry run python -m archershub \
  --username YOUR_ID \
  --password 'YOUR_PASSWORD' \
  --course-code LCFAITH \
  --target-section Z18 \
  --auto-switch-section
```

The default auto-switch strategy is `drop-add`. The script still requires manual captcha entry at login.
By default it tries to solve the captcha with Tesseract OCR first, then falls back to manual entry if OCR fails.

Install the Tesseract binary if it is not already available:

```bash
# macOS
brew install tesseract

# Termux / Android
pkg install tesseract
```

To force manual captcha entry:

```bash
--no-captcha-ocr
```

Optional reason IDs for Add/Drop, if required by ArchersHub:

```bash
--add-reason-id 5 --drop-reason-id 3
```

## Python endpoint client

The package also includes a mirror-derived endpoint catalog and a conservative
library client for ArchersHub AJAX endpoints:

```python
from archershub import ArchersHubClient

client = ArchersHubClient.from_env()
client.login()

important_dates = client.call("StudentDashboard/GetImportantDate")
profile = client.call("ProfileDetails/GetStudentPersonalDetails", params={"pagetabid": 1})
```

Configure credentials with environment variables:

```bash
export ARCHERSHUB_USERNAME='YOUR_ID'
export ARCHERSHUB_PASSWORD='YOUR_PASSWORD'
```

If captcha OCR misreads the image, the client retries login up to 5 times by
default. You can tune this or force manual captcha entry:

```bash
export ARCHERSHUB_MAX_LOGIN_ATTEMPTS=10
export ARCHERSHUB_NO_CAPTCHA_OCR=1
```

Mutation and payment endpoints are blocked by default. To call one, construct
the client with mutation support and confirm the exact endpoint:

```python
client = ArchersHubClient.from_env()
client.allow_mutation = True
client.call(
    "ApplyWithdrawal/DeleteWithdrawalById",
    params={"applyWithdrawalId": "123"},
    confirm_mutation="ApplyWithdrawal/DeleteWithdrawalById",
)
```

Live read-only endpoint tests can be run with:

```bash
ARCHERSHUB_USERNAME='YOUR_ID' \
ARCHERSHUB_PASSWORD='YOUR_PASSWORD' \
poetry run python -m unittest discover -s tests_live -v
```

## Safety notes

- Do not commit `cookies.json`, `captcha.png`, `login_result.html`, or course snapshots.
- Do not commit ArchersHub credentials or live-test artifacts.
- The endpoint client blocks mutation/payment calls unless explicitly enabled and confirmed.
- The script stops once the target section is reflected or accepted by the server.
- If a submit times out, it waits 10 seconds and checks server state before retrying.

## Telegram notification service

This repository now includes the first implementation pieces for a trusted small multi-user Telegram service.

### Configuration

Create a local `.env` file:

```env
BOT_TOKEN=123:telegram-token
ARCHERSHUB_MASTER_KEY=long-random-deployment-secret
ARCHERSHUB_DB=data/archershub_bot.sqlite3
```

Run the polling bot:

```bash
poetry run archershub-bot
```

The bot uses Telegram long polling, so a Raspberry Pi only needs outbound internet access. No webhook URL, public domain, Tailscale Funnel, Cloudflare Tunnel, or open router port is required.

If this bot previously had a webhook configured, startup clears it automatically before polling. To discard old queued Telegram updates on startup, add:

```env
TELEGRAM_DROP_PENDING_UPDATES=1
```

Minimal systemd service for a Raspberry Pi:

```ini
[Unit]
Description=ArchersHub Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/home/pi/archershub-endpoint
ExecStart=/usr/bin/env poetry run archershub-bot
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Admin CLI

```bash
poetry run archershub-admin init-db
poetry run archershub-admin generate-code --ttl-hours 24
poetry run archershub-admin list-users
poetry run archershub-admin list-jobs
poetry run archershub-admin list-failures
poetry run archershub-admin list-captcha-users
poetry run archershub-admin list-login-errors
poetry run archershub-admin list-pending
poetry run archershub-admin set-interval 30
```

### User flow and commands

After `/start <one-time-code>` and `/connect`, the bot shows a guided menu:

- **Add a class**: for a course you are not enlisted in yet. The bot tries priority sections first, then safe fallback sections. It never drops or changes existing classes to resolve conflicts.
- **Change section**: for a course you already have. The bot uses ArchersHub's change-section function only, never drop-add.

Power-user commands remain available:

- `/addclass LCFAITH:Z18,Z19` creates an add-class automation job.
- `/addclass LCFAITH:Z18,Z19 GETEAMS:S11 confirm` creates multiple add-class jobs and asks before submitting.
- `/change LCFAITH Z18` creates a change-section automation job.
- `/jobs` lists add/change jobs.
- `/remove 12` disables job `#12`.
- `/setmode 12 confirm` changes a job to `notify`, `confirm`, or `auto`.
- `/setpriorities 12 Z18 Z19` edits add-class priorities.
- `/retarget 13 Z20` edits a change-section target.
- `/confirm 12` executes a pending confirmation request after rechecking availability.
- `/reject 12` clears a pending confirmation request without disabling the job.
- `/cancel` cancels setup or a guided flow.

Mode behavior:

- `notify` sends an actionable alert once per newly-detected opportunity.
- `confirm` records a pending action and waits for `/confirm JOB_ID`.
- `auto` rechecks availability and submits immediately, then completes the job on success.

Change-section jobs use the existing change-section flow. Add-class jobs use the add/drop add-course flow, try priority sections first, skip clashing sections, and then fall back to normalized section-name order without displacing existing classes.

Captcha behavior:

- Bot logins use automated OCR only; they do not fall back to terminal prompts.
- Each automated login attempt loads a fresh login page and fresh captcha image.
- If automated captcha solving fails 5 times in a row, the bot sends the latest captcha image to the Telegram user.

Scheduler behavior:

- Failed jobs use exponential backoff before the next automatic retry.

Migration path:

- Postgres migration notes live in [docs/postgres-migration.md](/Users/armaine/Documents/projects/archershub-endpoint/docs/postgres-migration.md).
