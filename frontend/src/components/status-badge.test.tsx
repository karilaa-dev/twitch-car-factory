import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { StatusBadge } from "@/components/status-badge"

describe("StatusBadge", () => {
  it.each([
    ["running", "default"],
    ["stopped", "secondary"],
    ["restarting", "outline"],
    ["failed", "destructive"],
  ])("maps %s to the stock %s variant", (status, variant) => {
    render(<StatusBadge status={status} />)
    expect(screen.getByText(status)).toHaveAttribute("data-variant", variant)
  })
})
