# Twitch Farm feature and migration map

This document records the behavior preserved from the Telegram controller and
the runtime guarantees enforced by the Django replacement. It is the reference
for parity tests and future maintenance; the supported runtime is Django-only
and database-backed.

## Legacy runtime

The old `main.py` process loaded `config.yaml`, initialized JSON-backed state,
owned all miner subprocesses, started a Telegram polling loop, and ran a
60-second health task. Its persistent inputs were:

- `config.yaml`: Twitch account credentials, default channels, Telegram access,
  and the global autostart setting.
- `data/presets.json`: named ordered channel lists.
- `data/state.json`: account source selection, custom channels, and stale-prone
  `is_running`/`pid` fields.
- `cookies/`: Twitch miner session files.

Those files are accepted only inside the guarded ZIP flow at **Settings →
Import legacy data**. The Django runtime never synchronizes or mounts them.
SQLite stores accounts, encrypted credentials, defaults, presets, selections,
process intent, runtime observations, commands, runs, incidents, import
ownership, and audit history. Refreshable cookie sessions remain worker-owned
files under `runtime/cookies/`.

## Preserved feature map

| Legacy capability | Django replacement |
| --- | --- |
| Telegram ID whitelist | Django staff authentication; all staff share one farm |
| `/start` status menu | Shared dashboard with worker, account, queue, and incident status |
| Account list/detail | UI-managed active and archived accounts with public/detail disclosure |
| Add or change an account | Staff account create/edit forms with encrypted credentials |
| Start/stop/restart one account | Durable command targeting one active account |
| Start/stop all accounts | One durable command per eligible active account |
| Default channel source | Ordered defaults edited under Settings → General |
| Per-account custom channels | Ordered normalized SQLite rows |
| Named presets | Preset and ordered channel rows in SQLite |
| Assign preset from either view | Account selection or preset assignment forms |
| Restart after channel changes | Transactional restart command for affected desired-running accounts |
| Crash detection and restart | Supervisor incident plus bounded fast retry and indefinite degraded retry |
| Telegram failure messages | Persistent dashboard incidents and recovery history |
| Global autostart | UI setting for newly created accounts; later manual stop remains durable |
| One miner subprocess per account | One supervisor-owned child per desired-running account |
| Legacy migration | Staff-only ZIP upload, preview, explicit confirmation, and result report |
| Docker deployment | Separate migration, web, and singleton worker services |

Schedules were not wired into the active Telegram application and are not part
of this release.

## Account and secret ownership

The immutable account key identifies history and imported state. Staff may
change the public Twitch username and password, archive an account without
deleting its history, and reactivate an account once it has usable credentials
and channels.

Passwords are encrypted with the deployment Fernet keyring in a one-to-one
credential record. No UI, audit entry, launch snapshot, command, URL, or log
returns plaintext or ciphertext. Existing passwords are never pre-filled;
entering a new password replaces the old one and restarts the account when its
durable desired state is running.

An accepted imported cookie becomes normalized encrypted `AccountSessionSeed`
data, not retained pickle bytes. Only the worker can consume that one-time seed
and write a canonical mode-`0600` session file. The web service does not mount
the runtime cookie directory.

## Runtime ownership

The web process never starts, stops, signals, or adopts miner processes. It only
validates a request, changes durable desired state, and writes a command in the
same transaction. Exactly one `run_miner_worker` process:

1. Holds the local worker lock and updates its database lease.
2. Leases commands atomically and serializes work per account.
3. Resolves credentials and the current channel configuration from SQLite.
4. Creates an immutable launch record.
5. Places any one-time session seed and owns the resulting child process.
6. Reconciles desired state, observed state, configuration fingerprints, and
   unexpected exits until they converge.

## State model

Account activity and process state are separate. Archived accounts retain
history but are ineligible for starts, global lifecycle actions, and new preset
assignment. Archiving queues a durable stop. Reactivation does not implicitly
start an account.

Desired state is staff intent and survives restarts:

- `running`: there should be a healthy miner using the current effective
  channels. An unexpected exit must be recorded and recovered.
- `stopped`: the account was intentionally stopped. It must not auto-restart.

Observed state is worker-owned evidence:

- `unknown`: no current worker can vouch for the persisted observation.
- `starting`: a child was spawned but has not passed startup confirmation.
- `running`: the child passed startup confirmation and is still alive.
- `stopping`: a planned stop is in progress.
- `stopped`: no child is owned and desired state is stopped.
- `restarting`: a planned or recovery restart is in progress.
- `degraded`: desired state is running but rapid recovery attempts failed;
  periodic recovery continues.

Persisted PIDs are diagnostic only. A new worker clears them during startup and
never signals a PID unless it owns the corresponding live `Popen` object.

## Correct-channel guarantee

Before stopping a healthy process or spawning a new one, the worker reloads the
account, encrypted credential, selection, and default-channel settings from the
database. It rejects archived accounts, missing credentials, or empty effective
channel lists and creates an immutable `MinerRun` snapshot. The snapshot records
the ordered channels, source, account channel revision, and deterministic
configuration fingerprint. The child receives only the run and account IDs and
loads the launch data without secret-bearing process arguments.

A periodic reconciliation compares each running snapshot with the current
effective fingerprint. A mismatch causes a controlled restart, so a missed or
coalesced web command cannot leave an account watching stale channels.

## Recovery and incident rules

An exit is accidental only when the active account still has desired state
`running` and the worker did not request the stop. The worker closes the run,
opens one incident, and records every restart attempt. Fast recovery uses delays
of 5, 15, 30, 60, and 120 seconds. After those attempts, the account becomes
`degraded` and retries every five minutes until it recovers or a staff member
intentionally stops it. Ten healthy minutes reset the failure sequence.

Manual stops, configuration restarts, account edits, and graceful supervisor
shutdowns remain auditable run endings but are not incidents. A stale worker
lease is visible on the dashboard and is recorded as an unclean-worker incident
at the next startup.

## Legacy import contract

The Settings importer accepts one bounded ZIP, parses entries in memory, and
supports one common wrapping directory. It rejects unsafe paths, duplicate
normalized paths, links, encrypted or unsupported entries, excessive nesting,
and size/count limit violations. YAML and JSON use safe bounded parsers. Pickle
cookie input is treated as hostile and may contain only the narrow primitive
cookie structure the application normalizes.

Upload creates an encrypted, actor-bound draft with a sanitized preview. Drafts
expire after 30 minutes and can be confirmed once. Confirmation locks and
recomputes the database diff so a stale preview applies nothing. Imports never
start accounts; legacy `pid` and `is_running` fields are ignored. State-only
accounts are preserved archived and stopped.

Importer ownership prevents legacy data from overwriting UI-created records.
An identical intact import is a no-op. Replacement is limited to records owned
by a prior valid import and requires explicit acknowledgement plus `REPLACE`.
History-bearing accounts, in-use presets, and unrelated UI data are preserved.

## Intentional compatibility fixes

- Deleting an in-use preset is rejected instead of leaving a stale assignment.
- Canceling a form never persists partial edits.
- Names are not embedded in transport callback strings; web forms use database IDs.
- JSON whole-file writes are replaced with database transactions.
- Desired state is separate from observed process state.
- Account details are loaded on demand; preset assignment HTML initially shows
  only the public Twitch username.
- Plaintext passwords, cookie values, and authentication tokens are never
  rendered or passed in process arguments.
- A successful spawn does not erase crash history before a stable interval.
