import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"

import { DesktopOperationsGrid } from "@/pages/runtime-designs/operations-grid"
import type { AccountSummary, RuntimeSnapshot } from "@/types"

const account: AccountSummary = {
  id: 1,
  config_key: "primary",
  username: "primary_viewer",
  is_active: true,
  has_credentials: true,
  authentication: {
    method: "legacy_password",
    status: "authenticated",
    activation_url: "",
    user_code: "",
    expires_at: null,
    error: "",
    updated_at: "2026-07-24T12:00:00Z",
    can_reconnect: true,
  },
  desired: "running",
  observed: "running",
  source: {
    mode: "default",
    name: "Farm defaults",
    channels: ["offline", "online_only", "watched_b", "watched_a"],
  },
  watching_channels: ["watched_b", "watched_a"],
  online_channels: ["online_only", "watched_b", "watched_a"],
  watching_updated_at: "2026-07-24T12:00:00Z",
  pid: 42,
  last_heartbeat: "2026-07-24T12:00:00Z",
  open_incident: null,
  updated_at: "2026-07-24T12:00:00Z",
}

const snapshot: RuntimeSnapshot = {
  supervisor: {
    status: "healthy",
    label: "Supervisor online",
    owner_id: "worker-1",
    pid: 100,
    heartbeat_at: "2026-07-24T12:00:00Z",
    expires_at: "2026-07-24T12:01:00Z",
  },
  summary: {
    total: 1,
    desired_running: 1,
    observed_running: 1,
    degraded: 0,
    open_incidents: 0,
  },
  accounts: [account],
  incidents: [],
  command_faults: [],
  activity: [],
  generated_at: "2026-07-24T12:00:00Z",
}

describe("DesktopOperationsGrid", () => {
  it("groups watched, online, and offline channels in live account operations", () => {
    render(
      <MemoryRouter>
        <DesktopOperationsGrid
          data={snapshot}
          globalPending={false}
          accountPending={false}
          onGlobalAction={vi.fn()}
          onAccountAction={vi.fn()}
        />
      </MemoryRouter>
    )

    const channels = screen.getByLabelText(
      "Farming channels ordered by live status: watched, online, then offline"
    )
    expect(channels.textContent).toBe("watched_bwatched_aonline_onlyoffline")
    expect(
      screen.getByLabelText("watched_b (currently watched)")
    ).toHaveAttribute("data-variant", "success")
    expect(
      screen.getByLabelText("online_only (online, not currently watched)")
    ).toHaveAttribute("data-variant", "warning")
    expect(
      screen.getByLabelText("offline (offline or status unavailable)")
    ).toHaveAttribute("data-variant", "outline")
    expect(screen.queryByText(/^\+\d+$/)).not.toBeInTheDocument()
  })
})
