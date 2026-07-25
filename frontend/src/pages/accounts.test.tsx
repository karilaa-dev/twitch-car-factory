import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { AccountsPage, AccountWorkspacePage } from "@/pages/accounts"
import type { AccountDetail, AccountList, AccountTelemetry } from "@/types"

const apiMock = vi.fn()
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>()
  return { ...original, api: (...args: unknown[]) => apiMock(...args) }
})

const pendingAuthentication = {
  method: "twitch_tv" as const,
  status: "pending" as const,
  activation_url: "https://www.twitch.tv/activate",
  user_code: "FAKE-CODE",
  expires_at: new Date(Date.now() + 90_000).toISOString(),
  error: "",
  updated_at: new Date().toISOString(),
  can_reconnect: true,
}

const account: AccountDetail = {
  id: 1,
  config_key: "primary",
  username: "primary_viewer",
  is_active: true,
  has_credentials: true,
  authentication: pendingAuthentication,
  desired: "running",
  observed: "starting",
  source: {
    mode: "default",
    name: "farm defaults",
    channels: ["offline", "online", "one"],
  },
  watching_channels: ["one"],
  online_channels: ["online", "one"],
  watching_updated_at: new Date().toISOString(),
  planned_source: {
    mode: "default",
    name: "Farm defaults",
    channels: ["offline", "online", "one"],
  },
  pid: 42,
  last_heartbeat: new Date().toISOString(),
  open_incident: null,
  updated_at: new Date().toISOString(),
  configuration: {
    username: "primary_viewer",
    mode: "default",
    preset_id: null,
    channels: [],
  },
  presets: [],
  farm_default_channels: ["one"],
  incidents: [],
  runs: [],
  commands: [],
}

function renderWorkspace(tab: "auth" | "runtime") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/accounts/1?tab=${tab}`]}>
        <Routes>
          <Route path="/accounts/:id" element={<AccountWorkspacePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe("AccountsPage", () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockResolvedValue({
      accounts: [account],
      active_count: 1,
      presets: [],
      farm_default_channels: ["one"],
      autostart_new_accounts: false,
    } satisfies AccountList)
  })

  it("highlights watched channels in live account summaries", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <AccountsPage />
        </MemoryRouter>
      </QueryClientProvider>
    )

    const watchedBadges = await screen.findAllByLabelText(
      "one (currently watched)"
    )
    expect(watchedBadges.length).toBeGreaterThan(0)
    for (const badge of watchedBadges) {
      expect(badge).toHaveAttribute("data-variant", "success")
    }
  })
})

describe("Account Twitch TV authentication", () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockImplementation((path: string) => {
      if (path === "/accounts/1") return Promise.resolve(account)
      if (path === "/accounts/1/telemetry") {
        return Promise.resolve({
          account,
          planned_source: account.planned_source,
          generated_at: new Date().toISOString(),
        } satisfies AccountTelemetry)
      }
      throw new Error(`Unexpected API path: ${path}`)
    })
  })

  it("shows watched, online-only, and offline channels in runtime status order", async () => {
    renderWorkspace("runtime")

    expect(await screen.findByText("Runtime state")).toBeVisible()
    const channels = screen.getByLabelText(
      "Farming channels ordered by live status: watched, online, then offline"
    )
    expect(channels.textContent).toBe("oneonlineoffline")
    expect(
      screen.getByLabelText("one (currently watched)")
    ).toHaveAttribute("data-variant", "success")
    expect(
      screen.getByLabelText("online (online, not currently watched)")
    ).toHaveAttribute("data-variant", "warning")
  })

  it("shows and copies a pending activation code with the Twitch link", async () => {
    const user = userEvent.setup()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    })
    renderWorkspace("auth")

    expect(await screen.findByText("FAKE-CODE")).toBeVisible()
    expect(
      screen.getByRole("link", { name: /open twitch.tv\/activate/i })
    ).toHaveAttribute("href", "https://www.twitch.tv/activate")
    expect(screen.getByText(/expires in/i)).toBeVisible()
    await user.click(
      screen.getByRole("button", { name: "Copy activation code" })
    )
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("FAKE-CODE"))
  })
})
