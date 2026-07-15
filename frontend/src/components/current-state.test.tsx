import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import { CurrentState } from "@/components/current-state"
import { TooltipProvider } from "@/components/ui/tooltip"

describe("CurrentState", () => {
  it("shows only the live state and exposes different intent on hover", async () => {
    const user = userEvent.setup()
    render(
      <TooltipProvider>
        <CurrentState observed="starting" desired="running" />
      </TooltipProvider>
    )

    expect(screen.getByText("starting")).toBeVisible()
    const intent = screen.getByRole("button", {
      name: "Desired state: running",
    })
    expect(intent).toBeVisible()
    expect(intent.querySelector("svg")).toHaveClass("animate-spin")
    expect(intent.nextElementSibling).toHaveTextContent("starting")
    expect(screen.queryByText("running")).not.toBeInTheDocument()
    await user.hover(intent)
    expect(await screen.findByText("Desired state: running")).toBeVisible()
  })

  it("keeps stopped and broken states quiet", () => {
    const { rerender } = render(
      <TooltipProvider>
        <CurrentState observed="stopped" desired="running" />
      </TooltipProvider>
    )
    expect(screen.queryByRole("button")).not.toBeInTheDocument()

    rerender(
      <TooltipProvider>
        <CurrentState observed="degraded" desired="running" />
      </TooltipProvider>
    )
    expect(screen.queryByRole("button")).not.toBeInTheDocument()

    rerender(
      <TooltipProvider>
        <CurrentState observed="failed" desired="running" />
      </TooltipProvider>
    )
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("does not add an intent control when current and desired state match", () => {
    render(
      <TooltipProvider>
        <CurrentState observed="running" desired="running" />
      </TooltipProvider>
    )

    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })
})
