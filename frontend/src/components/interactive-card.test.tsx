import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import { MemoryRouter, useLocation } from "react-router-dom"

import { InteractiveCard } from "@/components/interactive-card"

function Location() {
  return <output>{useLocation().pathname}</output>
}

describe("InteractiveCard", () => {
  it("opens from the full card without hijacking nested actions", async () => {
    const user = userEvent.setup()
    const action = vi.fn()
    render(
      <MemoryRouter>
        <InteractiveCard to="/accounts/7" aria-label="Open account seven">
          Account seven
          <button onClick={action}>Restart</button>
        </InteractiveCard>
        <Location />
      </MemoryRouter>
    )

    await user.click(screen.getByRole("button", { name: "Restart" }))
    expect(action).toHaveBeenCalledOnce()
    expect(screen.getByRole("status")).toHaveTextContent("/")

    await user.click(screen.getByRole("link", { name: "Open account seven" }))
    expect(screen.getByRole("status")).toHaveTextContent("/accounts/7")
  })

  it("uses native link keyboard semantics", async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <InteractiveCard to="/accounts/7" aria-label="Open account seven">
          Account seven
        </InteractiveCard>
        <Location />
      </MemoryRouter>
    )
    const card = screen.getByRole("link", { name: "Open account seven" })
    card.focus()

    await user.keyboard(" ")
    expect(screen.getByRole("status")).toHaveTextContent("/")
    await user.keyboard("{Enter}")
    expect(screen.getByRole("status")).toHaveTextContent("/accounts/7")
  })
})
