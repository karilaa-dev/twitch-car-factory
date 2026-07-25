export type NoticeLevel = "info" | "success" | "warning" | "error"

export interface ApiNotice {
  level: NoticeLevel
  message: string
}

export interface ApiErrorBody {
  code: string
  message: string
  fields: Record<string, string[]>
}

export class ApiError extends Error {
  readonly code: string
  readonly fields: Record<string, string[]>
  readonly status: number

  constructor(body: ApiErrorBody, status: number) {
    super(body.message)
    this.name = "ApiError"
    this.code = body.code
    this.fields = body.fields
    this.status = status
  }
}

export interface ApiResponse<T> {
  data: T
  notices: ApiNotice[]
}

export interface SessionData {
  authenticated: boolean
  user: { id: number; username: string } | null
}

export type RuntimeStatus =
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "restarting"
  | "degraded"
  | "unknown"
  | "healthy"
  | "stale"
  | "offline"
  | "queued"
  | "leased"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "open"
  | "recovered"

export interface ChannelSource {
  mode: "default" | "custom" | "preset"
  name: string
  label?: string
  preset_id?: number | null
  channels: string[]
}

export interface AccountSummary {
  id: number
  config_key: string
  username: string
  is_active: boolean
  has_credentials: boolean
  authentication: {
    method: "twitch_tv" | "legacy_password"
    status: "unlinked" | "pending" | "authenticated" | "reauth_required"
    activation_url: string
    user_code: string
    expires_at: string | null
    error: string
    updated_at: string | null
    can_reconnect: boolean
  }
  desired: "running" | "stopped"
  observed: RuntimeStatus
  source: ChannelSource
  watching_channels: string[]
  online_channels: string[]
  watching_updated_at: string | null
  pid: number | null
  last_heartbeat: string | null
  open_incident: { id: number; summary: string; opened_at: string } | null
  updated_at: string
}

export interface Incident {
  id: number
  account_id: number | null
  account_key: string | null
  kind: string
  status: RuntimeStatus
  summary: string
  details: string
  opened_at: string
  recovered_at: string | null
  restart_attempts: Array<{
    attempt_number: number
    outcome: string
    scheduled_at: string
    error: string
  }>
}

export interface Command {
  id: number
  account_id: number
  account_key: string
  action: "start" | "stop" | "restart" | "authenticate"
  status: RuntimeStatus
  reason: string
  attempts: number
  error: string
  actor: string
  created_at: string
  completed_at: string | null
}

export interface Activity {
  id: number
  action: string
  message: string
  account_id: number | null
  account_key: string | null
  actor: string
  created_at: string
}

export interface Supervisor {
  status: RuntimeStatus
  label: string
  owner_id: string
  pid: number | null
  heartbeat_at: string | null
  expires_at: string | null
}

export interface RuntimeSnapshot {
  supervisor: Supervisor
  summary: {
    total: number
    desired_running: number
    observed_running: number
    degraded: number
    open_incidents: number
  }
  accounts: AccountSummary[]
  incidents: Incident[]
  command_faults: Command[]
  activity: Activity[]
  generated_at: string
}

export interface PresetSummary {
  id: number
  name: string
  channels: string[]
  watching_channels: string[]
  assignment_count: number
  updated_at: string
}

export interface AccountList {
  accounts: AccountSummary[]
  active_count: number
  presets: PresetSummary[]
  farm_default_channels: string[]
  autostart_new_accounts: boolean
}

export interface AccountDetail extends AccountSummary {
  planned_source: ChannelSource
  configuration: {
    username: string
    mode: ChannelSource["mode"]
    preset_id: number | null
    channels: string[]
  }
  presets: PresetSummary[]
  farm_default_channels: string[]
  incidents: Incident[]
  runs: Array<{
    id: number
    source_mode: string
    source_name: string
    channels: string[]
    channel_revision: number
    auth_method: "twitch_tv" | "legacy_password"
    reset_session: boolean
    pid: number | null
    started_at: string
    ended_at: string | null
    exit_code: number | null
    exit_signal: number | null
    stop_reason: string
    error: string
  }>
  commands: Command[]
}

export interface AccountTelemetry {
  account: AccountSummary
  planned_source: ChannelSource
  generated_at: string
}

export interface PresetDetail extends PresetSummary {
  assigned_account_ids: number[]
  eligible_accounts: Array<{
    id: number
    config_key: string
    username: string
    assigned: boolean
  }>
}

export interface FarmSettings {
  default_channels: string[]
  autostart_new_accounts: boolean
  updated_at: string
}

export interface ImportPreview {
  id: string
  expires_at: string
  created_at: string
  preview: {
    accounts: Record<string, Array<Record<string, unknown>>>
    presets: Record<string, Array<Record<string, unknown>>>
    cookies: Record<string, Array<Record<string, unknown>>>
    settings: {
      changed: boolean
      default_channels: string[]
      autostart_new_accounts: boolean
    }
    counts: Record<string, number>
    warnings: Array<{ message: string }>
    conflicts: Array<{ subject: string; message: string }>
    destructive_effects: Array<{ subject: string; message: string }>
    ignored_files: string[]
    requires_replace: boolean
    can_apply: boolean
    no_op: boolean
  }
}

export interface LogTail {
  lines: string[]
  line_count: number
  max_lines: number
  max_bytes: number
  cursor: string | null
  reset: boolean
  run_id: number | null
  source: {
    kind: "combined" | "account"
    account_id: number | null
    account_key: string | null
    username: string | null
  }
  supervisor: Supervisor
  generated_at: string
}

export interface LogRunSummary {
  run_id: number
  account: {
    id: number
    config_key: string
    username: string
    is_active: boolean
  }
  started_at: string
  ended_at: string | null
  stop_reason: string
  exit_code: number | null
  exit_signal: number | null
  archive_state: "active" | "compression_pending" | "ready"
  compressed_bytes: number
  compressed_parts: number
  plaintext_parts: number
  truncated: boolean
  downloadable: boolean
}

export interface LogRunList {
  runs: LogRunSummary[]
  next_before: string | null
  retention_bytes: number
  generated_at: string
}

export interface LogRunDetail {
  run: LogRunSummary
  lines: string[]
  line_count: number
  before: string | null
  has_older: boolean
  max_lines: number
  max_bytes: number
  generated_at: string
}
