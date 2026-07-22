# Twitch Farm Control Room

An admin-only Django control plane for running multiple Twitch Channel Points
Miner instances. SQLite is the source of truth for accounts, encrypted
credentials, channel settings, presets, process intent, and runtime history.
Accounts and application settings are managed through the web UI.

## Reliability model

- An admin **Stop** sets durable desired state to `stopped`; the account stays
  stopped across worker and container restarts.
- An unexpected process exit while desired state is `running` opens an incident
  and triggers supervised recovery.
- Each launch records an immutable ordered channel snapshot and fingerprint.
  The worker reconciles that fingerprint and restarts miners watching stale
  channels.
- The web process never owns subprocesses. One `run_miner_worker` process holds
  the singleton lock, leases commands, owns all child processes, and records
  runs, incidents, restart attempts, and recovery.
- Twitch passwords are encrypted at rest. Miner children receive account and run
  IDs, never passwords or channels in process arguments.

See [the feature and state map](docs/bot-feature-map.md) for full behavior.

## Architecture

```text
staff UI -> Django web -> SQLite accounts/settings/commands -> singleton worker
                                                               |-- miner child A
                                                               `-- miner child B
staff UI <- bounded log tail <- data/logs/twitch-farm.log <-----'

Settings -> legacy ZIP preview/confirm -> encrypted session seed -> worker
runtime/cookies/ (worker-only writable sessions) -----------------^
```

All Django staff accounts see and operate the same farm. The UI supports adding
and editing Twitch accounts, managing default/custom/preset channels, inspecting
combined worker and miner logs, and changing global settings. Existing inactive
legacy records remain visible for history but cannot be archived or reactivated
from the control room. Initial Django staff creation and encryption-key
provisioning remain deployment operations.

## Local setup

Requires Python 3.13, [uv](https://docs.astral.sh/uv/), and Node 24 with npm
for control-room frontend development. Production images build the frontend in
a Node stage and contain only the generated assets and Python runtime.
If you use nvm, run `nvm use` inside `frontend/` to select the version in
`.nvmrc`; other Node version managers can select Node 24 directly.

```bash
uv sync --frozen
export TWITCH_FARM_CREDENTIAL_KEYS="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
uv run python manage.py migrate
uv run python manage.py createsuperuser
cd frontend
npm ci
npm run build
cd ..
```

`createsuperuser` creates only the staff login for the control room. It does not
add a Twitch miner account; add Twitch accounts after signing in to the UI.

Run the web and worker in separate terminals with the same
`TWITCH_FARM_CREDENTIAL_KEYS` value:

```bash
uv run python manage.py runserver
TWITCH_FARM_LOG_WRITER=1 uv run python manage.py run_miner_worker
```

Open <http://127.0.0.1:8000/>, sign in with the staff account, configure default
channels under **Settings → General**, and add Twitch accounts from **Accounts**.

For live UI development, run `npm run build -- --watch` in `frontend/` and
Django in its normal terminal so API, authentication, and generated assets stay
same-origin. Populate seven fake accounts covering every observed
runtime state (starting, running, stopping, stopped, restarting, degraded, and
unknown):

```bash
uv run python manage.py seed_demo_data
```

The command is repeatable, uses only `demo-` account keys, and refuses to run
when `DJANGO_DEBUG` is disabled. The fake credentials are display fixtures and
must never be used with Twitch.

## Credential encryption keys

`TWITCH_FARM_CREDENTIAL_KEYS` is a comma-separated Fernet keyring. The first key
encrypts new values and every key may decrypt existing values. Generate a key
with:

```bash
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Production refuses to start without a valid explicit key. Store the keyring in
the same protected secret system used for `DJANGO_SECRET_KEY`, include it in
encrypted disaster-recovery backups, and provide the identical value to web,
worker, migration, and administrative processes. Losing every key that can
decrypt a record means that Twitch password cannot be recovered.

For rotation, prepend a new key and leave the prior keys in the list so new
values use the new key while existing values remain readable. This release has
no bulk re-encryption or key-retirement command. Keep prior keys unless you have
independently verified that every credential and pending session seed has been
rewritten or consumed, every outstanding import draft has expired, and legacy
import ownership metadata signed by that key is no longer needed. Import HMACs
accept every configured fallback key, so prepending a rotation key does not
invalidate active previews or owned rows. Re-saving an account password through
the UI rewrites that credential with the first key and restarts the miner when
its desired state is running.

## Import an old installation

The only supported legacy migration path is **Settings → Import legacy data**.
Stop the old application first so it cannot update files while the archive is
being made. Back up the old installation separately, then create one ZIP with
this layout (one common wrapping directory is also accepted):

```text
config.yaml
data/state.json
data/presets.json          # optional
cookies/<twitch_username>.pkl  # optional; use the username from config.yaml
```

Upload the ZIP in Settings, review the complete preview, and explicitly confirm
it. The importer ignores legacy PIDs and running flags, never starts accounts,
and reports skipped accounts or cookies without displaying their contents.
After confirmation, inspect the imported settings and start the desired
accounts from the UI.

Uploaded archives are limited to 10 MiB compressed, 25 MiB uncompressed, and
500 entries. The YAML file is limited to 256 KiB, each JSON file to 2 MiB, and
each cookie file to 256 KiB. Unknown files are ignored and identified in the
preview. Import previews expire after 30 minutes and are single-use. Legacy
cookie files are parsed as untrusted input; accepted cookie values are
normalized into an encrypted one-time seed. The worker consumes that seed on
first launch and writes the refreshable session beneath `runtime/cookies/`.

There is no command-line, startup, environment-variable, or Compose import
path. Do not mount old configuration or cookie directories into any service.

## Docker Compose

Create the environment file and the two protected bind-mount directories:

```bash
cp .env.example .env
# Replace both secret placeholders. Generate TWITCH_FARM_CREDENTIAL_KEYS with
# the Fernet command above and set PUID/PGID to the output of `id -u`/`id -g`.
chmod 600 .env
install -d -m 700 data runtime
docker compose run --rm migrate
docker compose run --rm --no-deps web python manage.py createsuperuser
docker compose up -d web worker
```

With the default `.env.example` values, the shipped port is bound to loopback
and serves direct HTTP at <http://127.0.0.1:8000/>. Secure cookies, HTTPS
redirects, and HSTS are disabled so login works without a TLS listener.

For external access, put Gunicorn behind a TLS-terminating reverse proxy such as
Caddy. Set `DJANGO_ALLOWED_HOSTS` to the public hostname,
`DJANGO_CSRF_TRUSTED_ORIGINS` to its full `https://` origin, and set
`DJANGO_SECURE_COOKIES=1` plus `DJANGO_SECURE_SSL_REDIRECT=1`. After HTTPS is
working reliably, set `DJANGO_SECURE_HSTS_SECONDS` to the intended policy
(commonly `31536000`). Enable HSTS subdomains or preload only when every affected
hostname is permanently HTTPS. The proxy must replace `X-Forwarded-Proto` with
the actual client scheme; Django trusts that header to identify secure requests.

The services share `./data`; only the worker mounts `./runtime`. The worker is
the sole log writer. It sends supervisor output and prefixed miner stdout/stderr
to Docker logs and the existing 5 MiB combined rotating file with three
backups. It also writes a private log for every miner run beneath
`./data/logs/accounts/<account-id>/runs/<run-id>/`. Active parts are bounded at
5 MiB, then compressed with gzip. Each account keeps at most 50 MiB of
published compressed parts; the oldest parts expire first, and the UI marks a
run when only its retained suffix remains. Completed run downloads are standard
multi-member `.log.gz` files.

The staff-only **Logs** page polls the live tail every two seconds and bounds
each response to 400 lines/256 KiB. Its History tab reads compressed parts by
account and run without exposing filesystem paths. If compression is
interrupted, the plaintext source remains in place, is shown as pending, and is
retried during worker reconciliation. New per-account collection starts after
deployment; existing combined rotations are not backfilled. The web service
can read `./data/logs` but remains unable to access refreshable Twitch session
files under `./runtime`.

Compose runs every service as the non-root `PUID`/`PGID` that owns the bind
mounts. SQLite must remain on local storage, and only one worker replica is
supported. The worker has a two-minute stop grace period so sequential miner
shutdown, log compression, and durable bookkeeping can finish before Docker
sends `SIGKILL`.

## Safe validation

The fake miner can exercise lifecycle and recovery without contacting Twitch:

```bash
uv run python manage.py run_fake_miner --help
```

Run the full validation suite:

```bash
cd frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
cd ..
uv run pytest -q
uv run python manage.py check
DJANGO_DEBUG=0 \
DJANGO_SECRET_KEY='use-a-random-value-at-least-50-characters-long-here' \
TWITCH_FARM_CREDENTIAL_KEYS='paste-a-generated-fernet-key-here' \
  uv run python manage.py check --deploy
uv run python manage.py makemigrations --check --dry-run
docker compose config --quiet
```

## Production notes

- Keep the direct Compose port on loopback. For external access, use the TLS
  proxy and explicit secure-setting opt-ins described above.
- Back up `data/db.sqlite3` with SQLite's online backup mechanism or while the
  services are stopped; copying a live WAL database as one file is not safe.
- Back up `TWITCH_FARM_CREDENTIAL_KEYS` independently of the database and limit
  both secrets to operators who need deployment access.
- Dashboard incidents replace Telegram alerts in this release. Email/webhook
  delivery and schedules remain out of scope.
- Rotate any credential that previously appeared in a tracked legacy file;
  deleting the file does not revoke an exposed password.
