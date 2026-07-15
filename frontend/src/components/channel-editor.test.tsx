import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"

import { ChannelEditor } from "@/components/channel-editor"

describe("ChannelEditor", () => {
  it("normalizes case-insensitive duplicates without changing order", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ChannelEditor id="channels" value={["alpha"]} onChange={onChange} />)
    await user.click(screen.getByRole("button", { name: "Add channel" }))
    const inputs = screen.getAllByPlaceholderText("twitch_channel")
    await user.type(inputs[1], "ALPHA")
    expect(onChange).toHaveBeenLastCalledWith(["alpha"])
    await user.clear(inputs[1])
    await user.type(inputs[1], "beta")
    expect(onChange).toHaveBeenLastCalledWith(["alpha", "beta"])
  })
})
