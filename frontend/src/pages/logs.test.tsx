import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { LogsPage } from "@/pages/logs"
import type { AccountList, LogRunDetail, LogRunList, LogTail } from "@/types"

const apiMock = vi.fn()

vi.mock("@/lib/api", () => ({
  api: (...args: unknown[]) => apiMock(...args),
  formatTime: (value: string | null | undefined) => value ?? "—",
  mutationError: vi.fn(),
}))

const accounts: AccountList = {
  accounts: [
    {
      id: 1,
      config_key: "primary",
      username: "Primary Farmer",
      is_active: true,
      has_credentials: true,
      desired: "running",
      observed: "running",
      source: { mode: "default", name: "farm defaults", channels: ["one"] },
      pid: 42,
      last_heartbeat: "2026-07-15T10:00:00Z",
      open_incident: null,
      updated_at: "2026-07-15T10:00:00Z",
    },
    {
      id: 2,
      config_key: "archived",
      username: "Archived Farmer",
      is_active: false,
      has_credentials: false,
      desired: "stopped",
      observed: "stopped",
      source: { mode: "custom", name: "archived", channels: ["two"] },
      pid: null,
      last_heartbeat: null,
      open_incident: null,
      updated_at: "2026-07-15T09:00:00Z",
    },
  ],
  active_count: 1,
  presets: [],
  farm_default_channels: ["one"],
  autostart_new_accounts: false,
}

function liveTail(lines: string[], cursor: string, accountId: number | null = null): LogTail {
  return {
    lines,
    line_count: lines.length,
    max_lines: 400,
    max_bytes: 262144,
    cursor,
    reset: false,
    run_id: accountId ? 55 : null,
    source: {
      kind: accountId ? "account" : "combined",
      account_id: accountId,
      account_key: accountId ? "archived" : null,
      username: accountId ? "Archived Farmer" : null,
    },
    supervisor: {
      status: "healthy",
      label: "Supervisor online",
      owner_id: "worker",
      pid: 1,
      heartbeat_at: "2026-07-15T10:00:00Z",
      expires_at: "2026-07-15T10:01:00Z",
    },
    generated_at: "2026-07-15T10:00:00Z",
  }
}

const run = {
  run_id: 55,
  account: {
    id: 2,
    config_key: "archived",
    username: "Archived Farmer",
    is_active: false,
  },
  started_at: "2026-07-14T10:00:00Z",
  ended_at: "2026-07-14T11:00:00Z",
  stop_reason: "unexpected_exit",
  exit_code: 1,
  exit_signal: null,
  archive_state: "ready" as const,
  compressed_bytes: 4096,
  compressed_parts: 2,
  plaintext_parts: 0,
  truncated: true,
  downloadable: true,
}

const history: LogRunList = {
  runs: [run],
  next_before: null,
  retention_bytes: 50 * 1024 * 1024,
  generated_at: "2026-07-15T10:00:00Z",
}

function detail(lines: string[], before: string | null): LogRunDetail {
  return {
    run,
    lines,
    line_count: lines.length,
    before,
    has_older: before !== null,
    max_lines: 400,
    max_bytes: 262144,
    generated_at: "2026-07-15T10:00:00Z",
  }
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  })
  return render(
    <QueryClientProvider client={client}>
      <LogsPage />
    </QueryClientProvider>
  )
}

describe("LogsPage", () => {
  beforeEach(() => {
    apiMock.mockReset()
  })

  it("appends cursor updates and switches the live source to an archived account", async () => {
    const user = userEvent.setup()
    let combinedCalls = 0
    apiMock.mockImplementation((path: string) => {
      if (path === "/accounts") return Promise.resolve(accounts)
      if (path === "/logs/runs/55?before=live-older") {
        return Promise.resolve(detail(["older-live-run-line"], null))
      }
      if (path === "/logs/runs/55") {
        return Promise.resolve(detail(["archived-account-line"], "live-older"))
      }
      if (path.startsWith("/logs?account_id=2")) {
        return Promise.resolve(liveTail(["archived-account-line"], "account-cursor", 2))
      }
      if (path.startsWith("/logs")) {
        combinedCalls += 1
        return Promise.resolve(
          combinedCalls === 1
            ? liveTail(["first-live-line"], "cursor-one")
            : liveTail(["second-live-line"], "cursor-two")
        )
      }
      throw new Error(`Unexpected API path: ${path}`)
    })

    renderPage()
    expect(
      await screen.findByText("first-live-line", {}, { timeout: 5_000 })
    ).toBeVisible()
    expect(
      await screen.findByText(/first-live-line.*second-live-line/s, {}, { timeout: 5_000 })
    ).toBeVisible()
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining("cursor=cursor-one"))

    await user.click(screen.getByRole("combobox", { name: "Log account" }))
    await user.click(await screen.findByRole("option", { name: /Archived Farmer/ }))
    expect(
      await screen.findByText("archived-account-line", {}, { timeout: 5_000 })
    ).toBeVisible()
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining("account_id=2"))
    await user.click(screen.getByRole("button", { name: "Load older lines from this run" }))
    expect(await screen.findByText(/older-live-run-line.*archived-account-line/s)).toBeVisible()
    expect(
      screen.queryByRole("button", { name: "Load older lines from this run" })
    ).not.toBeInTheDocument()
  }, 15_000)

  it("keeps a 2,000-line live buffer and does not auto-follow after scrolling away", async () => {
    const user = userEvent.setup()
    let liveCalls = 0
    apiMock.mockImplementation((path: string) => {
      if (path === "/accounts") return Promise.resolve(accounts)
      if (path.startsWith("/logs")) {
        const batch = liveCalls
        liveCalls += 1
        return Promise.resolve(
          liveTail(
            Array.from({ length: 400 }, (_, index) => `batch-${batch}-line-${index}`),
            `cursor-${batch}`
          )
        )
      }
      throw new Error(`Unexpected API path: ${path}`)
    })

    renderPage()
    await screen.findByText(/batch-0-line-0/, {}, { timeout: 5_000 })
    const viewport = screen
      .getByLabelText("Live farmer log lines")
      .closest<HTMLElement>("[data-slot='scroll-area-viewport']")
    expect(viewport).not.toBeNull()
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1_000 })
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 200 })
    if (!viewport) throw new Error("Log viewport did not render")
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    viewport.scrollTop = 100
    fireEvent.scroll(viewport)
    await new Promise((resolve) => window.setTimeout(resolve, 0))

    for (let batch = 1; batch <= 5; batch += 1) {
      await user.click(screen.getByRole("button", { name: "Refresh live logs" }))
      await screen.findByText(new RegExp(`batch-${batch}-line-399`), {}, { timeout: 5_000 })
    }

    const consoleText = screen.getByLabelText("Live farmer log lines").textContent ?? ""
    expect(consoleText).not.toContain("batch-0-line-0")
    expect(consoleText.split("\n")).toHaveLength(2_000)
    expect(viewport.scrollTop).toBe(100)
    await user.click(screen.getByRole("button", { name: "Jump to newest log line" }))
    expect(viewport.scrollTop).toBe(1_000)
  }, 15_000)

  it("shows older account lines at capacity and restores the newest live buffer", async () => {
    const user = userEvent.setup()
    let accountCalls = 0
    apiMock.mockImplementation((path: string) => {
      if (path === "/accounts") return Promise.resolve(accounts)
      if (path === "/logs/runs/55?before=older-page") {
        return Promise.resolve(detail(["two-factor-activation-code"], null))
      }
      if (path === "/logs/runs/55") {
        return Promise.resolve(detail(["newest-archive-line"], "older-page"))
      }
      if (path.startsWith("/logs?account_id=2")) {
        const batch = accountCalls
        accountCalls += 1
        return Promise.resolve(
          liveTail(
            Array.from({ length: 400 }, (_, index) => `account-batch-${batch}-line-${index}`),
            `account-cursor-${batch}`,
            2
          )
        )
      }
      if (path.startsWith("/logs")) return Promise.resolve(liveTail([], "combined-cursor"))
      throw new Error(`Unexpected API path: ${path}`)
    })

    renderPage()
    await user.click(await screen.findByRole("combobox", { name: "Log account" }))
    await user.click(await screen.findByRole("option", { name: /Archived Farmer/ }))
    await screen.findByText(/account-batch-0-line-399/, {}, { timeout: 5_000 })
    for (let batch = 1; batch <= 4; batch += 1) {
      await user.click(screen.getByRole("button", { name: "Refresh live logs" }))
      await screen.findByText(new RegExp(`account-batch-${batch}-line-399`), {}, { timeout: 5_000 })
    }
    expect(screen.getByLabelText("Live farmer log lines").textContent?.split("\n")).toHaveLength(2_000)

    await user.click(screen.getByRole("button", { name: "Load older lines from this run" }))
    expect(await screen.findByText(/two-factor-activation-code.*newest-archive-line/s)).toBeVisible()

    await user.click(screen.getByRole("button", { name: "Jump to newest log line" }))
    expect(await screen.findByText(/account-batch-4-line-399/)).toBeVisible()
    expect(screen.queryByText("two-factor-activation-code")).not.toBeInTheDocument()
  }, 15_000)

  it("shows truncated run history, pages older lines, and exposes the gzip download", async () => {
    const user = userEvent.setup()
    apiMock.mockImplementation((path: string) => {
      if (path === "/accounts") return Promise.resolve(accounts)
      if (path.startsWith("/logs/runs/55?before=older")) {
        return Promise.resolve(detail(["older-archived-line"], null))
      }
      if (path === "/logs/runs/55") return Promise.resolve(detail(["newest-archived-line"], "older"))
      if (path.startsWith("/logs/runs")) return Promise.resolve(history)
      if (path.startsWith("/logs")) return Promise.resolve(liveTail([], "cursor"))
      throw new Error(`Unexpected API path: ${path}`)
    })

    renderPage()
    await user.click(await screen.findByRole("tab", { name: /History/ }))
    expect(await screen.findByText("earlier lines expired")).toBeVisible()
    expect(await screen.findByText("newest-archived-line")).toBeVisible()
    const download = screen.getByRole("link", { name: /Download gzip/ })
    expect(download).toHaveAttribute("href", "/api/v1/logs/runs/55/download")

    await user.click(screen.getByRole("button", { name: "Load older lines" }))
    await waitFor(() => expect(screen.getByText(/older-archived-line.*newest-archived-line/s)).toBeVisible())
  })
})
