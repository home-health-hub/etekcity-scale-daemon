# etekcity-scale-daemon

A standalone Linux daemon that connects to an Etekcity smart fitness scale over Bluetooth Low Energy (BLE) and logs its measurements to a local SQLite database. No cloud account, no companion app, no Home Assistant required.

It's a thin wrapper around the [`etekcity_esf551_ble`](https://github.com/ronnnnnnnnnnnnn/etekcity_esf551_ble) library, packaged to run unattended as a `systemd` service on something like a Raspberry Pi sitting near the scale.

**Disclaimer: This is an unofficial, community-developed project. It is not affiliated with, officially maintained by, or in any way officially connected with Etekcity, VeSync Co., Ltd., or any of their subsidiaries or affiliates.**

## Supported scales

Whatever the underlying library supports at the time of installation:

| Model | Status |
|-------|--------|
| ESF-551 | Fully supported |
| EFS-A591S (Apex HR) | Experimental (adds heart rate) |
| ESF-24 | Experimental |
| FIT-8S | Experimental |

## Features

- Scans for any supported scale on first run, then pins its BLE address and model into the config file so future restarts connect directly instead of re-scanning
- Records every measurement (weight, impedance, and heart rate where available) to a local SQLite database
- Runs as a `systemd` service with automatic restart on failure
- No body-metrics calculation or cloud sync, just raw readings, timestamped

## Installation

Requires Python 3.11+.

### Quick install

```bash
git clone https://github.com/home-health-hub/etekcity-scale-daemon.git
cd etekcity-scale-daemon
sudo ./install.sh
```

This creates a venv at `/opt/etekcity-scale-daemon`, installs the package from the checkout, seeds `/etc/etekcity-scale-daemon/config.ini` (if it doesn't already exist), creates an `etekcity-scale-daemon` system user, and installs and enables the systemd service. It also installs (but does not enable) the [scheduled report generation](#scheduled-report-generation) and [alerting](#alerting) timer units, and the [HTTP API](#http-api) service. It's safe to re-run: it skips steps that are already done. Edit the config and `sudo systemctl restart etekcity-scale-daemon` afterward.

### Manual install

If you'd rather do it by hand or want to customize a step:

```bash
python3 -m venv /opt/etekcity-scale-daemon/venv
/opt/etekcity-scale-daemon/venv/bin/pip install /path/to/etekcity-scale-daemon  # this checkout
```

#### Config file

Copy the example config and edit it:

```bash
sudo mkdir -p /etc/etekcity-scale-daemon
sudo cp config/etekcity-scale-daemon.ini.example /etc/etekcity-scale-daemon/config.ini
sudo "$EDITOR" /etc/etekcity-scale-daemon/config.ini
```

Leave `[scale] address` and `model` empty to auto-discover a scale on first run (step on the scale while the daemon is scanning). Once found, the daemon writes the address and model back into this file so it reconnects directly on every future start.

| Section | Key | Description |
|---|---|---|
| `scale` | `address` | BLE MAC address of the scale. Leave empty to auto-discover. |
| `scale` | `model` | Scale model identifier. Filled in automatically after discovery. |
| `scale` | `adapter` | BLE adapter to use (Linux only), e.g. `hci0`. Leave empty for the default. |
| `scale` | `scanning_mode` | `active` or `passive` (Linux only). |
| `scale` | `cooldown_seconds` | Seconds to ignore advertisements after a disconnect (GATT-based scales only). |
| `storage` | `db_path` | Path to the SQLite database file. |
| `daemon` | `log_level` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `report` | `layout` | PDF layout: `full` (one row per reading), `simple` (date/weight only, in side-by-side columns), or `chart` (a line chart of weight over time). |
| `report` | `include_address` | Show the Address column in the `full` layout: `yes` or `no`. |
| `report` | `include_model` | Show the Model column in the `full` layout: `yes` or `no`. |
| `report` | `include_impedance` | Show the Impedance column in the `full` layout: `yes` or `no`. |
| `report` | `include_heart_rate` | Show the Heart Rate column in the `full` layout: `yes` or `no`. Only EFS-A591S reports heart rate; defaults to `no`. |
| `report` | `include_profile` | Show the Profile column in the `full` layout: `yes` or `no`. Mainly useful when a report spans every profile at once. Defaults to `no`. |
| `report` | `weight_unit` | Unit to render the Weight column in: `kg`, `lb`, or `st`. |
| `report` | `date_format` | `us` (MM/DD/YYYY, 12-hour) or `world` (DD/MM/YYYY, 24-hour). |
| `report` | `page_size` | PDF page size: `letter` or `a4`. |
| `report` | `include_summary` | Print a min/max/average/net-change summary line for Weight below the title: `yes` or `no`. Defaults to `no`. |
| `report` | `include_body_metrics` | Print a BMI/body-fat/etc. snapshot for the latest impedance reading: `yes` or `no`. Requires `--profile <name>`/`?profile=<name>` (see `profile.<name>` below). PDF only. Defaults to `no`. |
| `report` | `include_goal_progress` | Print a current/goal/remaining weight summary with a projected days-to-goal estimate, for the selected `--profile`/`?profile=`: `yes` or `no`. PDF only. Defaults to `no`. |
| `mqtt` | `enabled` | Publish each measurement to MQTT as JSON: `yes` or `no`. Defaults to `no`. |
| `mqtt` | `host` | Broker hostname. Required if `enabled = yes`. |
| `mqtt` | `port` | Broker port. Defaults to `1883`. |
| `mqtt` | `username` / `password` | Optional broker credentials. |
| `mqtt` | `use_tls` | Wrap the connection in TLS: `yes` or `no`. Defaults to `no`. |
| `mqtt` | `topic_prefix` | Messages publish to `<topic_prefix>/<scale address>/state`. Defaults to `etekcity_scale_daemon`. |
| `mqtt` | `qos` | MQTT QoS level: `0`, `1`, or `2`. Defaults to `0`. |
| `mqtt` | `retain` | Whether the broker retains the last message for new subscribers: `yes` or `no`. Defaults to `yes`. |
| `alerting` | `enabled` | Notify via Apprise on staleness/weight-swing conditions: `yes` or `no`. Defaults to `no`. |
| `alerting` | `apprise_urls` | Comma-separated [Apprise](https://github.com/caronc/apprise) service URLs. Required if `enabled = yes`. |
| `alerting` | `stale_after_days` | Alert if a scale hasn't reported in this many days. `0` disables the check. |
| `alerting` | `weight_swing_threshold_kg` | Alert if two consecutive readings differ by more than this many kg. `0` disables the check. |
| `alerting` | `state_path` | Where per-scale alert state is persisted (throttles repeat alerts). |
| `api` | `enabled` | Run the local HTTP API: `yes` or `no`. Defaults to `no`. |
| `api` | `host` | Bind address. Defaults to `127.0.0.1` (loopback only). |
| `api` | `port` | Bind port. Defaults to `8080`. |
| `api` | `token` | Optional bearer token required on all endpoints except `/api/v1/health` and `/api/v1/capabilities`. Blank means no auth. |
| `profiles` | `enabled` | Ask "who was this?" for a shared scale: `yes` or `no`. Defaults to `no`. |
| `profiles` | `names` | Comma-separated list of names to choose between. Required if `enabled = yes`. |
| `profiles` | `ntfy_url` | ntfy topic URL, e.g. `https://ntfy.sh/your-topic`. Required if `enabled = yes` and `[api] enabled = yes`. |
| `profiles` | `ntfy_token` | Optional ntfy access token. |
| `profiles` | `api_base_url` | Where this API is reachable, for ntfy's action buttons to call back into. |
| `profiles` | `dunstify_timeout_seconds` | Seconds to wait for a local dunstify response. Only used when `[api] enabled = no`. Defaults to `30`. |
| `profiles` | `assign_window_seconds` | Reject `/api/v1/assign-profile` requests tagging a reading older than this many seconds, unless `&confirm=1` is also passed. `0` disables the check. Defaults to `0`. |
| `profile.<name>` | `name` / `email` | Optional, printed below the title in PDF reports when this profile is selected. Leave blank to omit; `name` defaults to the profile's own name (e.g. `Alice`) if left blank. |
| `profile.<name>` | `height_unit` | Unit that `height` below is written in: `m`, `cm`, or `in`. Defaults to `m`. |
| `profile.<name>` | `height` / `birthdate` / `sex` / `athlete` | Required for this person's body composition if `report.include_body_metrics = yes`. One section per name in `profiles.names`. Never falls back to another profile's values. |
| `profile.<name>` | `skip_body_metrics` | `yes` or `no`. Skip body composition for this profile instead of requiring `height`/`birthdate`/`sex` -- for when impedance readings aren't physiologically meaningful for this person. Defaults to `no`. |
| `profile.<name>` | `weight_unit` | `kg`, `lb`, or `st`. Overrides `report.weight_unit` for this profile's reports only. Leave blank to just use `report.weight_unit`. |
| `profile.<name>` | `goal_weight_unit` / `goal_weight` | Unit (`kg`, `lb`, or `st`, defaults to `kg`) and value for `report.include_goal_progress`. Leave `goal_weight` blank to just not show goal progress for this profile -- no error, just a note. |

#### systemd service

```bash
sudo useradd --system --no-create-home --group etekcity-scale-daemon
sudo cp systemd/etekcity-scale-daemon.service /etc/systemd/system/
sudo ln -s /opt/etekcity-scale-daemon/venv/bin/etekcity-scale-daemon /usr/bin/etekcity-scale-daemon
sudo ln -s /opt/etekcity-scale-daemon/venv/bin/etekcity-scale-report /usr/bin/etekcity-scale-report
sudo ln -s /opt/etekcity-scale-daemon/venv/bin/etekcity-scale-prune /usr/bin/etekcity-scale-prune
sudo ln -s /opt/etekcity-scale-daemon/venv/bin/etekcity-scale-alert-check /usr/bin/etekcity-scale-alert-check
sudo ln -s /opt/etekcity-scale-daemon/venv/bin/etekcity-scale-api /usr/bin/etekcity-scale-api
sudo cp scripts/generate-scheduled-report.sh /opt/etekcity-scale-daemon/generate-scheduled-report.sh
sudo chmod +x /opt/etekcity-scale-daemon/generate-scheduled-report.sh
sudo ln -s /opt/etekcity-scale-daemon/generate-scheduled-report.sh /usr/bin/etekcity-scale-generate-report
sudo systemctl daemon-reload
sudo systemctl enable --now etekcity-scale-daemon
```

Watch the discovery step (first run) with:

```bash
sudo journalctl -u etekcity-scale-daemon -f
```

### Scheduled report generation

Optional and not enabled by default (`install.sh` installs the unit files but doesn't enable them). Generates a timestamped PDF to `/var/lib/etekcity-scale-daemon/reports/` on a schedule. There's no auto-email delivery, just a file dropped on disk, so wire up your own delivery (e.g. a script that watches the directory) if you need that.

```bash
sudo cp systemd/etekcity-scale-report-generate.service systemd/etekcity-scale-report-generate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now etekcity-scale-report-generate.timer
```

The timer defaults to `OnCalendar=weekly` (Monday 00:00). Edit `/etc/systemd/system/etekcity-scale-report-generate.timer` and `sudo systemctl daemon-reload` to change it. Override the config path or output directory with `ETEKCITY_CONFIG`/`ETEKCITY_REPORT_DIR` environment variables (add an `Environment=` line to the `.service` file's `[Service]` section). Check on it with:

```bash
sudo systemctl list-timers etekcity-scale-report-generate.timer
sudo journalctl -u etekcity-scale-report-generate.service
```

Prefer cron instead of systemd timers? Skip the timer unit and add a crontab entry calling the same wrapper directly:

```
0 0 * * 1 /usr/bin/etekcity-scale-generate-report
```

### Alerting

Also optional and not enabled by default. `etekcity-scale-alert-check` checks every scale's most recent readings for two conditions and notifies via [Apprise](https://github.com/caronc/apprise) (100+ supported services: Discord, Telegram, Slack, email, Pushover, generic webhooks, etc.) when triggered:

- **Staleness**: no reading in over `stale_after_days` days.
- **Weight swing**: the two most recent readings for a scale differ by more than `weight_swing_threshold_kg`.

Both are disabled (`0`) by default. Set at least one to a positive value, plus `apprise_urls`, in the `[alerting]` section:

```ini
[alerting]
enabled = yes
apprise_urls = tgram://bot_token/chat_id, mailto://user:password@gmail.com
stale_after_days = 2
weight_swing_threshold_kg = 5
```

Run it periodically with the bundled timer:

```bash
sudo cp systemd/etekcity-scale-alert-check.service systemd/etekcity-scale-alert-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now etekcity-scale-alert-check.timer
```

Defaults to `OnCalendar=hourly`. A repeat staleness alert is throttled to at most once per day while the condition persists; a weight-swing alert only fires once per newly-arrived reading, not on every check. State is tracked in `alerting.state_path` (default `/var/lib/etekcity-scale-daemon/alert-state.json`). Delete it to reset throttling. `--check-config` reports whether `[alerting]` is enabled and how many URLs it parsed, without actually sending anything.

### HTTP API

Also optional and not enabled by default. `etekcity-scale-api` runs a small read-only HTTP server exposing the same data as the other tools. It reads the SQLite database directly and works whether or not the daemon is currently running.

```ini
[api]
enabled = yes
host = 127.0.0.1
port = 8080
token =
```

```bash
sudo cp systemd/etekcity-scale-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now etekcity-scale-api.service
```

Endpoints, all under `/api/v1/`:

| Method & path | Description |
|---|---|
| `GET /api/v1/health` | Unauthenticated liveness check: `{"status": "ok", "version": "..."}`. |
| `GET /api/v1/capabilities` | Unauthenticated. What this running instance actually supports -- measurement types for the configured scale model, MQTT status, etc. -- so a consumer doesn't have to guess. |
| `GET /api/v1/latest[?address=...]` | Most recent reading for each scale (or one, if filtered), as JSON. |
| `GET /api/v1/report[?format=pdf\|csv&period=...&from=...&to=...&address=...&profile=...]` | Generates a report on demand using the same `[report]` config as `etekcity-scale-report`, returned as a file download. |

```bash
curl http://127.0.0.1:8080/api/v1/latest
curl -o report.pdf "http://127.0.0.1:8080/api/v1/report?period=30d"
```

**There's no TLS built in.** `host` defaults to `127.0.0.1` (loopback only) for a reason: don't bind it to `0.0.0.0` or a LAN-facing interface without putting a reverse proxy (with TLS and its own auth) in front of it. Setting `api.token` requires an `Authorization: Bearer <token>` header on every endpoint except `/api/v1/health` and `/api/v1/capabilities`, which is worth doing even on loopback if other local users/processes on the same host shouldn't see scale data:

```bash
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8080/api/v1/latest
```

### Profiles

For a scale shared by more than one person: `[profiles]` asks "who was this?" after each reading and tags it, so reports and body-metrics calculations (which otherwise assume one person, see the body-composition note above) can be filtered and computed per person. There's no way to identify who's standing on the scale automatically (no camera, no button, nothing but a weight number), so this asks a human directly instead of guessing from a weight range. Set `report.include_profile = yes` to show who each reading belongs to in a report that spans every profile at once, rather than filtering to just one via `--profile`/`?profile=`.

`--check-config` cross-checks `profiles.names` against the database: if a profile name was removed or renamed but readings tagged with the old name still exist, it prints a warning (not an error; the exit code stays `0`) so that history doesn't just silently stop being explainable.

Two delivery paths, chosen automatically based on whether `[api]` is enabled:

- **`[api]` enabled**: an [ntfy](https://ntfy.sh) notification with one HTTP action button per name in `profiles.names`. Tapping a button hits this API's `/api/v1/assign-profile` endpoint directly, tagging that specific reading. Requires `profiles.ntfy_url` (and `profiles.api_base_url` pointing at wherever the API is actually reachable from your phone/desktop; `127.0.0.1` only works if ntfy and the API run on the same machine). If the ntfy server is briefly unreachable or returns a 5xx error right when a reading happens, the publish is retried twice (after 1s, then 2s) before giving up and logging a warning. A 4xx response (bad token, bad request) is never retried.
- **`[api]` disabled**: a local [dunstify](https://dunst-project.org) prompt instead, since ntfy's action buttons would have nothing to call back to without the API running. This resolves synchronously and tags the reading directly, no network round-trip. It needs the `dunst` notification daemon and a real desktop/D-Bus session. It will not reach anywhere from a headless system service with no logged-in session, which is how the daemon runs by default.

```ini
[profiles]
enabled = yes
names = Alice, Bob
ntfy_url = https://ntfy.sh/your-topic
api_base_url = http://127.0.0.1:8080
```

`/api/v1/assign-profile` also accepts manual tagging or correcting a mistake:

```bash
curl "http://127.0.0.1:8080/api/v1/assign-profile?id=42&profile=Alice"
```

If `profiles.assign_window_seconds` is set, this fails with `409` for a reading older than that window: a safety net for delayed ntfy notifications (tapped long after connectivity returns, potentially tagging a now-stale reading someone's forgotten about) rather than a limit on manual corrections. Add `&confirm=1` to tag an old reading on purpose:

```bash
curl "http://127.0.0.1:8080/api/v1/assign-profile?id=42&profile=Alice&confirm=1"
```

#### Per-profile body composition

Give each profile its own `[profile.<name>]` section (name/email plus `height_unit`/`height`/`birthdate`/`sex`/`athlete`), and both the report CLI and the API can compute body composition for the right person:

```ini
[profile.Alice]
name = Alice Smith
email = alice@example.com
height_unit = cm
height = 165
birthdate = 1990-04-12
sex = female
athlete = no
```

```bash
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --profile Alice --output alice-report.pdf
curl -o alice-report.pdf "http://127.0.0.1:8080/api/v1/report?profile=Alice"
```

`--profile`/`?profile=` also filters which readings are included (only ones tagged with that name), not just which biometrics get used, and is required whenever `report.include_body_metrics = yes`, since there's no shared fallback section. This never falls back to another profile's values when a profile's section is missing or incomplete: that would mean silently computing Bob's body fat percentage using Alice's height, which is a correctness bug, not a convenience, so it's a clear error instead.

Impedance-based body composition assumes a complete electrical path through both feet, which doesn't hold for everyone: an amputee, someone standing on one foot, or anyone else who can't complete that path will get a physiologically meaningless number even though the weight reading itself is fine. Set `skip_body_metrics = yes` on that profile instead of filling in height/birthdate/sex, and reports print a note explaining why body composition was skipped rather than a bogus BMI:

```ini
[profile.Charlie]
skip_body_metrics = yes
```

A profile's `weight_unit` also overrides `report.weight_unit` for its own reports, so people sharing a scale don't have to agree on kg vs lb:

```ini
[profile.Bob]
weight_unit = lb
```

#### Goal progress

Set `report.include_goal_progress = yes` plus a `goal_weight` on a profile for a "Goal Progress" section: current weight, goal weight, how much remains, and a rough projected days-to-goal estimate from the rate of change across every weighed reading in the report's range (a linear regression, not just the first and last point, so one outlier reading can't single-handedly swing the estimate).

```ini
[report]
include_goal_progress = yes

[profile.Alice]
goal_weight_unit = kg
goal_weight = 62
```

Unlike body composition, there's no correctness risk in a profile simply not having a goal set, so a missing `goal_weight`, or generating a report with no `--profile`/`?profile=` at all, just prints a note instead of erroring.

Why ntfy specifically: unlike most notification services, ntfy's `http` action type is a full HTTP request (URL, method, headers, body) fired directly when the button is tapped, so the notification service itself is the callback mechanism, no separate bot or polling process needed. Pushover only supports a single acknowledge callback tied to emergency-priority alerts, Pushbullet's actionable notifications are about mirroring your own devices rather than third-party callbacks, and Gotify has no equivalent at all. [Apprise](https://github.com/caronc/apprise) (used for [alerting](#alerting)) isn't used here either, since its unified API has no concept of actions and targets 100+ services, most of which have nothing like this.

### Docker

**⚠️ Unverified.** Docker wasn't available in the environment this was written in, so the image has only been checked for "does `pip install .` succeed with these files." The container has never actually been built, started, or tested against real BLE hardware. Treat this as a starting point to debug, not a working install path, until someone confirms it end-to-end.

BLE access from inside a container needs the host's D-Bus system bus and Bluetooth adapter, which is why `docker-compose.yml` uses `network_mode: host` plus a bind mount of `/var/run/dbus`; bridge networking would isolate the container from both.

A pre-built image publishes to GHCR from CI on every push to `main`, tagged `latest` and by commit SHA, so `docker pull ghcr.io/home-health-hub/etekcity-scale-daemon:latest` works instead of building locally, if you'd rather not build it yourself. Substitute that image name for `etekcity-scale-daemon` in the commands below to use it instead of `docker build`.

```bash
mkdir -p config data
cp config/etekcity-scale-daemon.ini.example config/config.ini
"$EDITOR" config/config.ini   # set storage.db_path = /var/lib/etekcity-scale-daemon/measurements.db
docker compose up -d --build
docker compose logs -f
```

Run `etekcity-scale-report` or `etekcity-scale-prune` inside the running container:

```bash
docker compose exec etekcity-scale-daemon etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --output /var/lib/etekcity-scale-daemon/report.pdf
```

Without Compose, the equivalent is:

```bash
docker build -t etekcity-scale-daemon .
docker run -d --name etekcity-scale-daemon \
  --network host \
  -v /var/run/dbus:/var/run/dbus \
  -v "$(pwd)/config:/etc/etekcity-scale-daemon" \
  -v "$(pwd)/data:/var/lib/etekcity-scale-daemon" \
  --restart unless-stopped \
  etekcity-scale-daemon
```

## Manual usage

```bash
etekcity-scale-daemon --config /etc/etekcity-scale-daemon/config.ini
etekcity-scale-daemon --config /etc/etekcity-scale-daemon/config.ini --verbose
```

Validate a config file (all sections) without starting the daemon:

```bash
etekcity-scale-daemon --config /etc/etekcity-scale-daemon/config.ini --check-config
```

### On-demand capture instead of a long-running service

`--once` records a single measurement and exits, instead of running until stopped: for when you'd rather not run the daemon continuously, start it by hand right before (or while) stepping on the scale. It waits up to `--once-timeout` seconds (default 60) for a reading and exits non-zero if none arrives in time:

```bash
etekcity-scale-daemon --config /etc/etekcity-scale-daemon/config.ini --once --once-timeout 30
```

On the very first run, if `[scale] address`/`model` are still empty, `--once` also uses `--once-timeout` as the scale-discovery timeout (instead of a separate 60-second default). Worst case, an undiscovered scale can take up to `2 * --once-timeout` before giving up. Once discovered, the address is saved back into the config (as always), so every run after that only waits `--once-timeout` for the measurement itself.

## Database schema

Each measurement is inserted as one row into the `measurements` table:

| Column | Type | Notes |
|---|---|---|
| `recorded_at` | TEXT | ISO-8601 UTC timestamp |
| `address` | TEXT | Scale's BLE MAC address |
| `model` | TEXT | Scale model identifier |
| `weight_kg` | REAL | Weight in kilograms |
| `impedance_ohms` | REAL | Bio-impedance, if reported |
| `impedance_500khz_ohms` | REAL | ESF-24 only: 500 kHz impedance |
| `heart_rate_bpm` | REAL | EFS-A591S only |
| `display_unit` | TEXT | Scale's displayed unit at time of reading |

Query it directly with `sqlite3`, or point any BI/graphing tool at the file.

## Reports

`etekcity-scale-report` reads the database and writes a table of readings to a PDF or CSV file:

```bash
# Every reading on record
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --output report.pdf

# Preset ranges: 7d, 30d, 90d, 1y, all (default: all)
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --period 30d --output last-30-days.pdf

# Explicit date range (--to defaults to now if omitted)
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --from 2026-01-01 --to 2026-03-31 --output q1.pdf

# Point directly at a database file instead of a config
etekcity-scale-report --db /var/lib/etekcity-scale-daemon/measurements.db --output report.pdf
```

Add `--address AA:BB:CC:DD:EE:FF` to restrict the report to one scale if the database has readings from more than one.

If the database has readings from more than one scale (e.g. different family members each with their own), add `--multi-scale` instead to get one PDF with a separate heading, its own table/chart, and its own summary line per scale, each starting on a fresh page, rather than one table mixing everyone's readings together:

```bash
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --multi-scale --output all-scales.pdf
```

`--multi-scale` is mutually exclusive with `--address` and only affects PDF output (`--format csv` ignores it, since the CSV's `Address` column already differentiates scales in one flat file).

Add `--format csv` for a CSV file instead of a PDF (default output path becomes `measurements-report.csv`):

```bash
etekcity-scale-report --config /etc/etekcity-scale-daemon/config.ini --format csv --output report.csv
```

CSV export always uses the `full` layout's column set (respecting `include_address`/`include_model`/`include_impedance`/`include_heart_rate`/`include_profile`, `weight_unit`, and `date_format`). `layout`, `page_size`, `include_summary`, `include_body_metrics`, and profile name/email/biometrics are PDF-only and have no effect on CSV.

Set `report.include_body_metrics = yes` and pass `--profile <name>`/`?profile=<name>` (see [Per-profile body composition](#per-profile-body-composition)) for a "Body Composition" section: BMI, body fat %, muscle mass, bone mass, and the rest of the upstream library's `BodyMetrics` calculations, computed from the single most recent reading that has impedance data. It's a current snapshot, not a per-reading history, and only applies to single-scale PDF reports. It's skipped for `--format csv` (no profile context there) and for `--multi-scale` (which can represent readings from different people, so one shared height/birthdate/sex wouldn't make sense).

The layout, which columns appear, the weight unit, and the date/time format are all controlled by the `[report]` section of the config file (see the table above). `--config` reads them; `--db` always uses the defaults (`full` layout, all columns, kilograms, `world` date format).

The `simple` layout drops every column except Date/Time and Weight and lays readings out in several side-by-side column pairs (filling one pair top-to-bottom before starting the next) instead of a single narrow two-column table.

The `chart` layout replaces the table with a line chart of weight over time (x-axis labels thin themselves out automatically when there are many readings). It needs at least two readings with a weight value; with fewer, the page prints a "not enough data" note instead. `include_address`/`include_model`/`include_impedance`/`include_heart_rate` have no effect on this layout; only `weight_unit` and `date_format` apply.

See [samples/](samples/) for a rendered PDF of every layout/unit/date-format combination.

Set a profile's `name` and/or `email` (only usable via `--profile`/`?profile=`, and only read from `--config`, not `--db`) to print that identifying info below the title, handy when handing a report to a doctor. Leave either blank to omit it; leave both blank and no patient line is printed at all.

## Pruning old data

`etekcity-scale-prune` deletes measurements older than a given number of days. It's manual only: nothing in the daemon deletes data automatically. It's a **dry run by default**: it reports how many rows match without touching anything, until you pass `--yes`.

```bash
# See how many readings older than 365 days would be deleted
etekcity-scale-prune --config /etc/etekcity-scale-daemon/config.ini --older-than 365

# Actually delete them (also reclaims disk space with VACUUM)
etekcity-scale-prune --config /etc/etekcity-scale-daemon/config.ini --older-than 365 --yes
```

Add `--address AA:BB:CC:DD:EE:FF` to restrict pruning to one scale. `--db` works the same as with `etekcity-scale-report`, bypassing the config file.

## MQTT

Set `[mqtt] enabled = yes` (plus `host`) to publish each measurement to an MQTT broker as JSON, alongside the local SQLite recording:

```ini
[mqtt]
enabled = yes
host = broker.example.com
port = 1883
username = myuser
password = mypassword
use_tls = no
topic_prefix = etekcity_scale_daemon
qos = 0
retain = yes
```

Each measurement publishes to `<topic_prefix>/<scale address>/state`, e.g. `etekcity_scale_daemon/AA:BB:CC:DD:EE:FF/state`, with the same fields stored in the database:

```json
{"recorded_at": "2026-08-06T01:23:45.678901+00:00", "address": "AA:BB:CC:DD:EE:FF", "model": "ESF-551", "weight_kg": 75.0, "impedance_ohms": 500.0, "impedance_500khz_ohms": null, "heart_rate_bpm": null, "display_unit": "KG"}
```

A broker that's down or unreachable is logged as a warning and otherwise ignored. BLE recording to the local database is the daemon's primary job, and it's never blocked by an MQTT outage. Check `--check-config` to confirm the daemon parsed your `[mqtt]` settings as expected before relying on it.

There's no Home Assistant MQTT discovery support yet (auto-creating entities): this publishes raw JSON only. Subscribe and parse it yourself, or wire up discovery messages separately if you need that.

## Troubleshooting

On Raspberry Pi (and other BlueZ-based Linux systems), a `org.bluez.Error.InProgress` error usually clears up with:

```
bluetoothctl power off
bluetoothctl power on
bluetoothctl scan on
```

## Acknowledgments

- Scale hardware designed and sold by [Etekcity](https://www.etekcity.com) / [VeSync Co., Ltd.](https://www.vesync.com) (see the Disclaimer above).
- Built on [`etekcity_esf551_ble`](https://github.com/ronnnnnnnnnnnnn/etekcity_esf551_ble) by maintainer [@ronnnnnnnnnnnnn](https://github.com/ronnnnnnnnnnnnn), which does all the BLE protocol and reverse-engineering work.
- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/home-health-hub/etekcity-scale-daemon/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/home-health-hub/etekcity-scale-daemon/discussions).
- Pull requests are welcome for bug fixes or discussed features.

CI (`.github/workflows/ci.yml`) runs `flake8`, the `pytest` suite under `tests/`, and `scripts/smoke-test.sh` on every PR. Run them locally before pushing:

```bash
pip install flake8
flake8 --config .flake8 src tests
pip install ".[test]"
pytest tests/
./scripts/smoke-test.sh
```

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
