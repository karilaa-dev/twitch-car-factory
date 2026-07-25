import { describe, expect, it } from "vitest"

import { needsRuntimeAttention } from "@/lib/runtime-state"
import type { AccountSummary, RuntimeStatus } from "@/types"

function account(
  observed: RuntimeStatus,
  desired: AccountSummary["desired"],
  patch: Partial<AccountSummary> = {}
): AccountSummary {
  return {
    id: 1,
    config_key: "primary",
    username: "viewer",
    is_active: true,
    has_credentials: true,
    authentication: {
      method: "legacy_password",
      status: "authenticated",
      activation_url: "",
      user_code: "",
      expires_at: null,
      error: "",
      updated_at: "2026-07-15T00:00:00Z",
      can_reconnect: true,
    },
    desired,
    observed,
    source: { mode: "default", name: "default", channels: [] },
    watching_channels: [],
    online_channels: [],
    watching_updated_at: null,
    pid: null,
    last_heartbeat: null,
    open_incident: null,
    updated_at: "2026-07-15T00:00:00Z",
    ...patch,
  }
}

describe("needsRuntimeAttention", () => {
  it("ignores only transitions moving toward the desired state", () => {
    expect(needsRuntimeAttention(account("starting", "running"))).toBe(false)
    expect(needsRuntimeAttention(account("restarting", "running"))).toBe(false)
    expect(needsRuntimeAttention(account("stopping", "stopped"))).toBe(false)
    expect(needsRuntimeAttention(account("starting", "stopped"))).toBe(true)
    expect(needsRuntimeAttention(account("stopping", "running"))).toBe(true)
    expect(needsRuntimeAttention(account("stopped", "running"))).toBe(true)
  })

  it("keeps incidents, faults, and blocked starts visible", () => {
    expect(needsRuntimeAttention(account("failed", "running"))).toBe(true)
    expect(
      needsRuntimeAttention(
        account("running", "running", {
          open_incident: { id: 1, summary: "Lost heartbeat", opened_at: "now" },
        })
      )
    ).toBe(true)
    expect(
      needsRuntimeAttention(
        account("stopped", "running", { has_credentials: false })
      )
    ).toBe(true)
  })
})
