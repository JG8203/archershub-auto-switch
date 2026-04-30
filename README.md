# ArchersHub Auto Switch

Python utility for logging in to ArchersHub, monitoring a course section, and automatically switching to a target section when a slot opens.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python login_requests.py \
  --username YOUR_ID \
  --password 'YOUR_PASSWORD' \
  --course-code LCFAITH \
  --target-section Z18 \
  --auto-switch-section
```

The default auto-switch strategy is `drop-add`. The script still requires manual captcha entry at login.

Optional reason IDs for Add/Drop, if required by ArchersHub:

```bash
--add-reason-id 5 --drop-reason-id 3
```

## Safety notes

- Do not commit `cookies.json`, `captcha.png`, `login_result.html`, or course snapshots.
- The script stops once the target section is reflected or accepted by the server.
- If a submit times out, it waits 10 seconds and checks server state before retrying.
