#!/usr/bin/env bash

set -Eeuo pipefail

IMAGE="${1:?usage: docker_crash_recovery_smoke.sh IMAGE}"
SUFFIX="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$"
RESOURCE_PREFIX="twitch-farm-smoke-$SUFFIX"
DATA_VOLUME="$RESOURCE_PREFIX-data"
MIGRATE_CONTAINER="$RESOURCE_PREFIX-migrate"
SEED_CONTAINER="$RESOURCE_PREFIX-seed"
WORKER="$RESOURCE_PREFIX-worker"
TRACKED_CONTAINERS=(
  "$WORKER"
  "$SEED_CONTAINER"
  "$MIGRATE_CONTAINER"
)
DOCKER_TIMEOUT_SECONDS="${SMOKE_DOCKER_TIMEOUT_SECONDS:-20}"
DOCKER_CLEANUP_TIMEOUT_SECONDS="${SMOKE_DOCKER_CLEANUP_TIMEOUT_SECONDS:-10}"

if [[ ! "$DOCKER_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || [[ ! "$DOCKER_CLEANUP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Smoke Docker timeouts must be positive whole seconds." >&2
  exit 1
fi

run_with_timeout() {
  local timeout_seconds="$1"
  local description="$2"
  shift 2
  local command_pid watchdog_pid command_status watchdog_status

  "$@" &
  command_pid=$!
  (
    local sleeper_pid=""
    trap 'if [[ -n "$sleeper_pid" ]]; then kill "$sleeper_pid" >/dev/null 2>&1 || true; fi; exit 0' INT TERM
    sleep "$timeout_seconds" &
    sleeper_pid=$!
    if ! wait "$sleeper_pid"; then
      exit 0
    fi
    trap '' INT TERM
    if kill -0 "$command_pid" >/dev/null 2>&1; then
      kill -TERM "$command_pid" >/dev/null 2>&1 || true
      sleep 2
      kill -KILL "$command_pid" >/dev/null 2>&1 || true
      exit 124
    fi
    exit 0
  ) &
  watchdog_pid=$!

  if wait "$command_pid"; then
    command_status=0
  else
    command_status=$?
  fi
  if kill -0 "$watchdog_pid" >/dev/null 2>&1; then
    kill -TERM "$watchdog_pid" >/dev/null 2>&1 || true
  fi
  if wait "$watchdog_pid" 2>/dev/null; then
    watchdog_status=0
  else
    watchdog_status=$?
  fi
  if (( watchdog_status == 124 )); then
    echo "Timed out after ${timeout_seconds}s: $description" >&2
    return 124
  fi
  return "$command_status"
}

dump_failure_state() {
  if ! run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" "inspect worker for diagnostics" \
    docker container inspect "$WORKER" >/dev/null 2>&1; then
    return
  fi

  echo "::group::Miner worker logs"
  run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" "read worker logs" \
    docker logs "$WORKER" 2>&1 || true
  echo "::endgroup::"

  if [[ "$(run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" "inspect worker state" \
    docker container inspect --format '{{.State.Running}}' "$WORKER" 2>/dev/null || true)" == "true" ]]; then
    echo "::group::Miner controller state"
    run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" "read controller diagnostics" \
      docker exec "$WORKER" python manage.py shell -c \
      "from controller.models import MinerCommand,MinerIncident,MinerInstanceState,MinerRun,RestartAttempt; print('states', list(MinerInstanceState.objects.values('account__config_key','desired_state','observed_state','current_run_id','advisory_pid','retry_count','next_retry_at','last_error'))); print('commands', list(MinerCommand.objects.values('id','action','status','attempts','error'))); print('runs', list(MinerRun.objects.values('id','channels','startup_confirmed_at','ended_at','exit_code','exit_signal','stop_reason'))); print('incidents', list(MinerIncident.objects.values('id','kind','status','run_id','recovered_at'))); print('restart_attempts', list(RestartAttempt.objects.values('id','incident_id','run_id','attempt_number','outcome','error')))" \
      2>&1 || true
    echo "::endgroup::"
  fi
}

remove_tracked_container() {
  local container="$1"
  local output status
  if output="$(run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" \
    "remove container $container" docker rm --force "$container" 2>&1)"; then
    return 0
  else
    status=$?
  fi
  if grep -qi "No such container" <<<"$output"; then
    return 0
  fi
  printf '%s\n' "$output" >&2
  return "$status"
}

remove_tracked_volume() {
  local output status
  if output="$(run_with_timeout "$DOCKER_CLEANUP_TIMEOUT_SECONDS" \
    "remove volume $DATA_VOLUME" docker volume rm "$DATA_VOLUME" 2>&1)"; then
    return 0
  else
    status=$?
  fi
  if grep -qi "no such volume" <<<"$output"; then
    return 0
  fi
  printf '%s\n' "$output" >&2
  return "$status"
}

cleanup() {
  local status=$?
  local cleanup_failed=0
  local container
  trap - EXIT

  if (( status != 0 )); then
    echo "Built-image crash/recovery smoke failed with status $status." >&2
    dump_failure_state
  fi

  for container in "${TRACKED_CONTAINERS[@]}"; do
    if ! remove_tracked_container "$container"; then
      cleanup_failed=1
    fi
  done
  if ! remove_tracked_volume; then
    cleanup_failed=1
  fi
  if (( cleanup_failed != 0 )); then
    echo "Built-image smoke cleanup did not remove every tracked artifact." >&2
    if (( status == 0 )); then
      status=1
    fi
  fi
  if (( status == 0 )); then
    echo "Built-image fresh-volume crash/recovery smoke passed."
  fi
  exit "$status"
}
trap cleanup EXIT

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "create smoke data volume" \
  docker volume create "$DATA_VOLUME" >/dev/null

COMMON_ARGS=(
  --env DJANGO_SECRET_KEY=smoke-only-secret-abcdefghijklmnopqrstuvwxyz-0123456789
  --env DJANGO_DEBUG=0
  --env TWITCH_FARM_CREDENTIAL_KEYS=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
  --env TWITCH_FARM_DB=/app/data/db.sqlite3
  --env TWITCH_FARM_RUNTIME_DIR=/app/runtime
  --env TWITCH_FARM_WORKER_LOCK=/app/data/miner-worker.lock
  --volume "$DATA_VOLUME:/app/data"
)

wait_for_state() {
  local description="$1"
  local timeout_seconds="$2"
  local query="$3"
  local deadline=$((SECONDS + timeout_seconds))
  local output=""

  while (( SECONDS < deadline )); do
    if output="$(
      run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "query $description" \
        docker exec "$WORKER" python manage.py shell -c "$query" 2>/dev/null
    )" && grep -q '^READY$' <<<"$output"; then
      return 0
    fi

    if [[ "$(run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "inspect worker while waiting" \
      docker container inspect --format '{{.State.Running}}' "$WORKER" 2>/dev/null || true)" != "true" ]]; then
      echo "Worker stopped while waiting for $description." >&2
      return 1
    fi
    sleep 0.25
  done

  echo "Timed out after ${timeout_seconds}s waiting for $description." >&2
  return 1
}

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "run migrations" \
  docker run --name "$MIGRATE_CONTAINER" "${COMMON_ARGS[@]}" \
  "$IMAGE" python manage.py migrate --noinput >/dev/null
run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "seed database account and enqueue start" \
  docker run --name "$SEED_CONTAINER" "${COMMON_ARGS[@]}" \
  "$IMAGE" python manage.py shell -c \
  "from controller.services import create_account,enqueue_command,update_farm_configuration; update_farm_configuration(default_channels=['channel_one','channel_two'],autostart_new_accounts=False); account=create_account(config_key='primary',username='smoke_twitch_user',password='smoke-password-not-in-output',mode='default',start_after_save=False); enqueue_command(account,'start',reason='built-image smoke')" \
  >/dev/null

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "start miner worker" \
  docker run --detach --name "$WORKER" "${COMMON_ARGS[@]}" \
  --env TWITCH_FARM_FAKE_MINER=1 \
  --env TWITCH_FARM_FAKE_MINER_MODE=normal \
  --env TWITCH_FARM_FAKE_MINER_RECORD_FILE=/app/runtime/fake-miner.jsonl \
  --env MINER_COMMAND_POLL_SECONDS=0.1 \
  --env MINER_HEALTH_POLL_SECONDS=0.1 \
  --env MINER_FINGERPRINT_POLL_SECONDS=60 \
  --env MINER_STARTUP_GRACE_SECONDS=0.5 \
  --env MINER_RAPID_RESTART_BACKOFF=0.25,0.5,1,1,1 \
  --env MINER_DEGRADED_RETRY_SECONDS=2 \
  --env MINER_WORKER_HEARTBEAT_SECONDS=0.1 \
  "$IMAGE" python manage.py run_miner_worker --interval 0.1 \
  >/dev/null

wait_for_state "the initial confirmed launch" 30 \
  "from controller.models import MinerCommand,MinerInstanceState; s=MinerInstanceState.objects.select_related('current_run').get(account__config_key='primary'); r=s.current_run; ok=s.desired_state=='running' and s.observed_state=='running' and r is not None and r.startup_confirmed_at is not None and r.channels==['channel_one','channel_two'] and MinerCommand.objects.filter(account=s.account,action='start',status='succeeded').exists(); print('READY' if ok else 'WAIT')"

FIRST_RUN_ID="$(
  run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "read initial run ID" \
    docker exec "$WORKER" python manage.py shell -c \
    "from controller.models import MinerInstanceState; print(MinerInstanceState.objects.get(account__config_key='primary').current_run_id)" \
    | tail -n 1
)"
if [[ ! "$FIRST_RUN_ID" =~ ^[1-9][0-9]*$ ]]; then
  echo "Could not determine the initial run ID: $FIRST_RUN_ID" >&2
  exit 1
fi

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "kill initial fake miner" \
  docker exec "$WORKER" python manage.py shell -c \
  "import os,signal; from controller.models import MinerInstanceState; state=MinerInstanceState.objects.get(account__config_key='primary'); assert state.advisory_pid; os.kill(state.advisory_pid,signal.SIGKILL)" \
  >/dev/null

wait_for_state "incident recovery and the replacement launch" 45 \
  "from controller.models import MinerIncident,MinerInstanceState,MinerRun,RestartAttempt; s=MinerInstanceState.objects.select_related('current_run','account').get(account__config_key='primary'); r=s.current_run; old=MinerRun.objects.get(pk=$FIRST_RUN_ID); inc=MinerIncident.objects.filter(account=s.account,kind='unexpected_exit').order_by('-id').first(); ok=bool(r and r.pk!=old.pk and r.startup_confirmed_at and r.channels==['channel_one','channel_two'] and s.desired_state=='running' and s.observed_state=='running' and old.ended_at and old.stop_reason=='unexpected_exit' and old.exit_signal==9 and inc and inc.run_id==old.pk and inc.status=='recovered' and inc.recovered_at and RestartAttempt.objects.filter(incident=inc,run=r,outcome='succeeded').exists()); print('READY' if ok else 'WAIT')"

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "verify fake-miner launch records" \
  docker exec "$WORKER" python -c \
  "import json; from pathlib import Path; rows=[json.loads(line) for line in Path('/app/runtime/fake-miner.jsonl').read_text().splitlines()]; encoded=json.dumps(rows); assert len(rows)==2, rows; assert all(row['account_key']=='primary' and row['channels']==['channel_one','channel_two'] and not any('password' in key.casefold() for key in row) for row in rows); assert 'smoke-password-not-in-output' not in encoded; assert rows[0]['run_id'] != rows[1]['run_id']" \
  >/dev/null
run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "verify fake-miner argv" \
  docker exec "$WORKER" python manage.py shell -c \
  "from pathlib import Path; from controller.models import MinerInstanceState; state=MinerInstanceState.objects.get(account__config_key='primary'); argv=[part for part in Path(f'/proc/{state.advisory_pid}/cmdline').read_bytes().split(b'\\0') if part]; assert argv[-3]==b'run_fake_miner' and argv[-1]==str(state.account_id).encode(); assert not any(b'channel_one' in part or b'channel_two' in part or b'smoke-password-not-in-output' in part or part.startswith(b'--channel') or part.startswith(b'--password') for part in argv)" \
  >/dev/null

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "enqueue manual stop" \
  docker exec "$WORKER" python manage.py shell -c \
  "from controller.models import MinerAccount; from controller.services import enqueue_command; enqueue_command(MinerAccount.objects.get(config_key='primary'),'stop',reason='built-image smoke cleanup')" \
  >/dev/null

wait_for_state "durable manual-stop convergence" 30 \
  "from controller.models import MinerCommand,MinerIncident,MinerInstanceState,MinerRun; s=MinerInstanceState.objects.get(account__config_key='primary'); replacement=MinerRun.objects.exclude(pk=$FIRST_RUN_ID).order_by('-id').first(); ok=s.desired_state=='stopped' and s.observed_state=='stopped' and s.current_run_id is None and s.advisory_pid is None and replacement is not None and replacement.ended_at is not None and replacement.stop_reason=='admin_stop' and MinerCommand.objects.filter(account=s.account,action='stop',status='succeeded').exists() and MinerIncident.objects.filter(account=s.account,kind='unexpected_exit',status='recovered').count()==1; print('READY' if ok else 'WAIT')"

run_with_timeout "$DOCKER_TIMEOUT_SECONDS" "stop miner worker" \
  docker stop --time 5 "$WORKER" >/dev/null
