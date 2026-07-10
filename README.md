# Twitch Farm Control Room

An admin-only Django control plane for running multiple Twitch Channel Points
Miner instances. SQLite stores shared controller state while `config.yaml`
remains the source of truth for Twitch credentials, default channels, and the
initial autostart policy.

## Reliability model

- An admin **Stop** sets durable desired state to `stopped`; the account stays
  stopped across worker and container restarts.
- An unexpected process exit while desired state is `running` opens an incident
  and triggers supervised recovery.
- Each launch records an immutable ordered channel snapshot and fingerprint.
  The worker periodically reconciles that fingerprint with current configuration
  and restarts a miner that is watching stale channels.
- The web process never owns subprocesses. One `run_miner_worker` process holds
  the singleton lock, leases commands, owns all child processes, and records
  runs, incidents, restart attempts, and recovery.
- Twitch passwords stay in YAML. Children receive only a run ID and account key,
  never a password in argv.

See [the feature and state map](docs/bot-feature-map.md) for full behavior.

## Architecture

```text
admins -> Django web -> SQLite command/state tables -> singleton worker
                                                        |-- miner child A
                                                        `-- miner child B

config.yaml ---------- credentials/defaults ------------^
cookies/ (read-only seed) -------------------------------^
runtime/cookies/ (worker-only writable sessions) --------^
```

All Django staff accounts see and operate the same farm. Twitch accounts are
not editable in the web panel; change `config.yaml`, then restart the worker or
run `sync_config_accounts`.

## Local setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cp config.yaml.example config.yaml
uv sync --frozen
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

For a new installation, synchronize YAML-backed accounts:

```bash
uv run python manage.py sync_config_accounts
```

Run the web and worker in separate terminals:

```bash
uv run python manage.py runserver
uv run python manage.py run_miner_worker
```

Open <http://127.0.0.1:8000/> and sign in with a staff account.

## Configuration

```yaml
settings:
  autostart_instances: false

twitch_users:
  primary:
    username: "your_twitch_username"
    password: "YOUR_PASSWORD_HERE"

default_channels:
  - "channel_one"
  - "channel_two"
```

`autostart_instances` seeds desired-running only when an account is first
discovered or imported. It does not undo a later intentional admin stop.

Keep `config.yaml` mode `0600` and `cookies/` mode `0700`. Neither belongs in
Git or the container image.

## Migrate legacy JSON

Stop the Telegram controller before importing so it cannot launch miners or
rewrite JSON concurrently. Back up `config.yaml`, `data/`, `cookies/`, and any
refreshed `runtime/cookies/` into a protected directory such as `backups/`
that is not mounted into the web container, then:

```bash
uv run python manage.py migrate
uv run python manage.py import_legacy_data --config config.yaml --data-dir data --dry-run
uv run python manage.py import_legacy_data --config config.yaml --data-dir data
```

The importer migrates presets, custom channels, and account selections. Legacy
`pid` and `is_running` values are deliberately ignored. State-only account keys
are retained as unconfigured/non-runnable records so data is not silently lost.
An identical rerun is safe; use `--replace` only when intentionally replacing
changed imported controller data.

## Docker Compose

```bash
cp .env.example .env
# Set a strong DJANGO_SECRET_KEY.
# Set PUID/PGID in .env to the output of `id -u` and `id -g`.
install -d -m 700 data cookies runtime
docker compose run --rm migrate
# Create the first Django staff login before starting the web and worker services.
docker compose run --rm --no-deps web python manage.py createsuperuser
docker compose --profile tools run --rm importer \
  python manage.py import_legacy_data --config /app/config.yaml --data-dir /app/data --dry-run
docker compose --profile tools run --rm importer
docker compose up -d web worker
```

The shipped port is bound to loopback and serves direct HTTP. With the default
`.env.example` values, open <http://127.0.0.1:8000/>; secure cookies, HTTPS
redirects, and HSTS are deliberately disabled so login works without a TLS
listener.

For external access, put Gunicorn behind a TLS-terminating reverse proxy such
as Caddy. Set `DJANGO_ALLOWED_HOSTS` to the public hostname,
`DJANGO_CSRF_TRUSTED_ORIGINS` to its full `https://` origin, and set
`DJANGO_SECURE_COOKIES=1` plus `DJANGO_SECURE_SSL_REDIRECT=1`. After HTTPS is
working reliably, set `DJANGO_SECURE_HSTS_SECONDS` to the intended policy
(commonly `31536000`). Enable HSTS subdomains or preload only when every
affected hostname is permanently HTTPS. The proxy must replace
`X-Forwarded-Proto` with the actual client scheme; Django trusts that header to
identify secure requests.

The services share `./data`, but only the worker/importer mount `config.yaml`
and the original cookies; those secret-source mounts are read-only. Each miner
copies its seed cookie into the worker-only `runtime/cookies/` mount and runs
there so the upstream library can refresh sessions without modifying the
backup. The web container cannot mount or read those Twitch session files.
Compose runs every service as the non-root `PUID`/`PGID` that owns the
bind-mounted files. SQLite must remain on local storage, and only one worker
replica is supported.

## Safe validation

The fake miner can exercise lifecycle and recovery without Twitch credentials:

```bash
uv run python manage.py run_fake_miner --help
```

Run the full validation suite:

```bash
uv run pytest -q
uv run python manage.py check
DJANGO_DEBUG=0 DJANGO_SECRET_KEY='use-a-random-value-at-least-50-characters-long-here' \
  uv run python manage.py check --deploy
uv run python manage.py makemigrations --check --dry-run
docker compose config --quiet
```

## Production notes

- Keep the direct Compose port on loopback. For external access, use the TLS
  proxy and explicit secure-setting opt-ins described above.
- Back up `data/db.sqlite3` with SQLite's online backup mechanism or while the
  services are stopped; copying a live WAL database as one file is not safe.
- Dashboard incidents replace Telegram alerts in this release. Email/webhook
  delivery and schedules are intentionally out of scope.
- Rotate any credential that previously appeared in the tracked legacy
  `instance.py`; deleting the file does not revoke an exposed password.
