import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { ChannelList } from "@/components/channel-list"

describe("ChannelList", () => {
  it("separates farming channels into monospace badges and summarizes overflow", () => {
    render(<ChannelList channels={["alpha", "beta", "gamma"]} limit={2} />)

    expect(screen.getByText("alpha")).toHaveClass("font-mono")
    expect(screen.getByText("beta")).toHaveClass("font-mono")
    expect(screen.getByText("+1")).toBeVisible()
    expect(screen.queryByText("gamma")).not.toBeInTheDocument()
  })
})
