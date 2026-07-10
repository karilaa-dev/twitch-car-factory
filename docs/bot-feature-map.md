# Twitch Farm feature and migration map

This document records the behavior preserved from the Telegram controller and the
runtime guarantees enforced by the Django replacement. It is the reference for
parity tests and future maintenance; the supported runtime is Django-only.

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

The Django rewrite intentionally keeps Twitch credentials, default channels,
and the initial autostart policy in `config.yaml`. Presets, selections, process
intent, runtime observations, commands, runs, incidents, and audit history move
to SQLite. Cookies remain files.

## Preserved feature map

| Legacy capability | Django replacement |
| --- | --- |
| Telegram ID whitelist | Django staff authentication; all staff share one farm |
| `/start` status menu | Shared dashboard with worker, account, queue, and incident status |
| Account list/detail | Config-mirrored account list and operational detail |
| Start/stop/restart one account | Durable command targeting one account |
| Start/stop all accounts | One durable command per configured account |
| Default channel source | Ordered defaults loaded from YAML |
| Per-account custom channels | Ordered normalized SQLite rows |
| Named presets | Preset and ordered channel rows in SQLite |
| Assign preset from either view | Account selection or preset assignment forms |
| Restart after channel changes | Transactional restart command for affected desired-running accounts |
| Crash detection and restart | Supervisor incident plus bounded fast retry and indefinite degraded retry |
| Telegram failure messages | Persistent dashboard incidents and recovery history |
| Global autostart | Seeds new/imported accounts; later manual stop remains durable |
| One miner subprocess per account | One supervisor-owned child per desired-running account |
| Docker deployment | Separate migration, web, and singleton worker services |

Schedules were not wired into the active Telegram application and are not part
of this release.

## Runtime ownership

The web process never starts, stops, signals, or adopts miner processes. It only
validates a request, changes durable desired state, and writes a command in the
same transaction. Exactly one `run_miner_worker` process:

1. Holds the local worker lock and updates its database lease.
2. Leases commands atomically and serializes work per account.
3. Resolves and validates the current channel configuration.
4. Creates an immutable launch record.
5. Owns the resulting child process and all signals sent to it.
6. Reconciles desired state, observed state, configuration fingerprints, and
   unexpected exits until they converge.

## State model

Desired state is admin intent and survives restarts:

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

Before stopping a healthy process or spawning a new one, the worker reloads
`config.yaml`, resolves the latest selection, rejects missing credentials or an
empty effective channel list, and creates an immutable `MinerRun` snapshot. The
snapshot records the ordered channels, source, account channel revision, and a
deterministic configuration fingerprint. The child receives only the run ID and
account key; it reads channels from that snapshot and credentials from YAML.

A periodic reconciliation compares each running snapshot with the current
effective fingerprint. A mismatch causes a controlled restart, so a missed or
coalesced web command cannot leave an account watching stale channels.

## Recovery and incident rules

An exit is accidental only when the account still has desired state `running`
and the worker did not request the stop. The worker closes the run, opens one
incident, and records every restart attempt. Fast recovery uses delays of 5, 15,
30, 60, and 120 seconds. After those attempts, the account becomes `degraded`
and retries every five minutes until it recovers or an admin intentionally stops
it. Ten healthy minutes reset the failure sequence.

Manual stops, configuration restarts, and graceful supervisor shutdowns remain
auditable run endings but are not incidents. A stale worker lease is visible on
the dashboard and is recorded as an unclean-worker incident at the next startup.

## Intentional compatibility fixes

- Deleting an in-use preset is rejected instead of leaving a stale assignment.
- Canceling a form never persists partial edits.
- Names are not embedded in transport callback strings; web forms use database IDs.
- JSON whole-file writes are replaced with database transactions.
- Desired state is separate from observed process state.
- Twitch passwords are never stored in SQLite or passed in process arguments.
- A successful spawn does not erase crash history before a stable interval.
