import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { PresetsPage } from "@/pages/presets"
import type { PresetSummary } from "@/types"

const apiMock = vi.fn()
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>()
  return { ...original, api: (...args: unknown[]) => apiMock(...args) }
})

const preset: PresetSummary = {
  id: 1,
  name: "Evening rotation",
  channels: ["Alpha", "beta"],
  watching_channels: ["alpha"],
  assignment_count: 2,
  updated_at: "2026-07-24T12:00:00Z",
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <PresetsPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe("PresetsPage", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    apiMock.mockReset()
    apiMock.mockResolvedValue({ presets: [preset] })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("polls presets and identifies currently watched channels", async () => {
    renderPage()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(apiMock).toHaveBeenCalledWith("/presets")
    expect(screen.getByLabelText("Channel status legend")).toHaveTextContent(
      "Currently watched"
    )
    expect(screen.getByLabelText("Alpha (currently watched)")).toHaveAttribute(
      "data-variant",
      "success"
    )
    expect(
      screen.getByLabelText("beta (not currently watched)")
    ).toHaveAttribute("data-variant", "outline")

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(apiMock).toHaveBeenCalledTimes(2)
  })
})
