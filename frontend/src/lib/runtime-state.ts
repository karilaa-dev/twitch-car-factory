import type { AccountSummary, RuntimeStatus } from "@/types"

const problemStates = new Set<RuntimeStatus>([
  "degraded",
  "failed",
  "offline",
  "stale",
  "unknown",
])

function isProgressingTowardDesiredState(account: AccountSummary) {
  if (account.desired === "running") {
    return account.observed === "starting" || account.observed === "restarting"
  }
  return account.observed === "stopping"
}

export function needsRuntimeAttention(account: AccountSummary) {
  if (account.open_incident || problemStates.has(account.observed)) return true
  if (
    account.desired === "running" &&
    (!account.is_active || !account.has_credentials)
  )
    return true
  if (account.observed === account.desired) return false
  return !isProgressingTowardDesiredState(account)
}
