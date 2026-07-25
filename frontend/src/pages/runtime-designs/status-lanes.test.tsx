import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"
import { MemoryRouter } from "react-router-dom"

import { MobileStatusLanes } from "@/pages/runtime-designs/status-lanes"
import type { AccountSummary, RuntimeSnapshot } from "@/types"

function account(
  id: number,
  overrides: Partial<AccountSummary> = {}
): AccountSummary {
  return {
    id,
    config_key: `account_${id}`,
    username: `account_${id}`,
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
    desired: "running",
    observed: "running",
    source: {
      mode: "default",
      name: "Farm defaults",
      channels: ["channel_one"],
    },
    watching_channels: [],
    online_channels: [],
    watching_updated_at: null,
    pid: id,
    last_heartbeat: "2026-07-15T12:00:00Z",
    open_incident: null,
    updated_at: "2026-07-15T12:00:00Z",
    ...overrides,
  }
}

function snapshot(accounts: AccountSummary[]): RuntimeSnapshot {
  return {
    supervisor: {
      status: "healthy",
      label: "Supervisor online",
      owner_id: "worker-1",
      pid: 100,
      heartbeat_at: "2026-07-15T12:00:00Z",
      expires_at: "2026-07-15T12:01:00Z",
    },
    summary: {
      total: accounts.length,
      desired_running: accounts.filter((item) => item.desired === "running")
        .length,
      observed_running: accounts.filter((item) => item.observed === "running")
        .length,
      degraded: 0,
      open_incidents: 0,
    },
    accounts,
    incidents: [],
    command_faults: [],
    activity: [],
    generated_at: "2026-07-15T12:00:00Z",
  }
}

describe("MobileStatusLanes", () => {
  it("omits operating-state lanes that have no accounts", () => {
    render(
      <MemoryRouter>
        <MobileStatusLanes
          data={snapshot([account(1)])}
          globalPending={false}
          accountPending={false}
          onGlobalAction={vi.fn()}
          onAccountAction={vi.fn()}
        />
      </MemoryRouter>
    )

    expect(screen.getByRole("heading", { name: "Running" })).toBeVisible()
    expect(
      screen.queryByRole("heading", { name: "Needs attention" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("heading", { name: "Stopped or idle" })
    ).not.toBeInTheDocument()
  })

  it("groups each account's watched, online, and offline channels", () => {
    render(
      <MemoryRouter>
        <MobileStatusLanes
          data={snapshot([
            account(1, {
              source: {
                mode: "default",
                name: "Farm defaults",
                channels: ["offline", "Online_Only", "watched_b", "watched_a"],
              },
              watching_channels: ["WATCHED_A", "watched_b"],
              online_channels: ["online_only", "watched_b", "watched_a"],
            }),
          ])}
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
    expect(channels.textContent).toBe("watched_bwatched_aOnline_Onlyoffline")
    expect(
      screen.getByLabelText("watched_a (currently watched)")
    ).toHaveAttribute("data-variant", "success")
    expect(
      screen.getByLabelText("Online_Only (online, not currently watched)")
    ).toHaveAttribute("data-variant", "warning")
    expect(
      screen.getByLabelText("offline (offline or status unavailable)")
    ).toHaveAttribute("data-variant", "outline")
  })

  it("keeps each populated lane visible", () => {
    render(
      <MemoryRouter>
        <MobileStatusLanes
          data={snapshot([
            account(1, { observed: "degraded" }),
            account(2),
            account(3, { desired: "stopped", observed: "stopped" }),
          ])}
          globalPending={false}
          accountPending={false}
          onGlobalAction={vi.fn()}
          onAccountAction={vi.fn()}
        />
      </MemoryRouter>
    )

    expect(
      screen.getByRole("heading", { name: "Needs attention" })
    ).toBeVisible()
    expect(screen.getByRole("heading", { name: "Running" })).toBeVisible()
    expect(
      screen.getByRole("heading", { name: "Stopped or idle" })
    ).toBeVisible()
  })
})
