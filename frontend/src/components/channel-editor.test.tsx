import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ChannelEditor } from "@/components/channel-editor"
import { api } from "@/lib/api"

vi.mock("@/lib/api", () => ({ api: vi.fn() }))

const apiMock = vi.mocked(api)

describe("ChannelEditor", () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockResolvedValue({ name: "alpha", status: "exists" })
  })

  it("normalizes case-insensitive duplicates without changing order", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ChannelEditor id="channels" value={["alpha"]} onChange={onChange} />
    )
    await user.click(screen.getByRole("button", { name: "Add channel" }))
    const inputs = screen.getAllByPlaceholderText("twitch_channel")
    await user.type(inputs[1], "ALPHA")
    expect(onChange).toHaveBeenLastCalledWith(["alpha"])
    await user.clear(inputs[1])
    await user.type(inputs[1], "beta")
    expect(onChange).toHaveBeenLastCalledWith(["alpha", "beta"])
  })

  it("ignores validation results for a channel edited while the request is pending", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    let resolveValidation!: (value: { name: string; status: "exists" }) => void
    apiMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveValidation = resolve
      })
    )
    render(
      <ChannelEditor id="channels" value={["old_name"]} onChange={onChange} />
    )
    const input = screen.getByDisplayValue("old_name")

    fireEvent.blur(input)
    await waitFor(() => expect(apiMock).toHaveBeenCalledOnce())
    await user.clear(input)
    await user.type(input, "new_name")
    await act(async () => {
      resolveValidation({ name: "canonical_old", status: "exists" })
    })

    expect(input).toHaveValue("new_name")
    expect(onChange).toHaveBeenLastCalledWith(["new_name"])
  })

  it("publishes the accepted canonical channel name", async () => {
    const onChange = vi.fn()
    apiMock.mockResolvedValueOnce({ name: "canonical", status: "exists" })
    render(
      <ChannelEditor id="channels" value={["Canonical"]} onChange={onChange} />
    )

    fireEvent.blur(screen.getByDisplayValue("Canonical"))

    await waitFor(() =>
      expect(screen.getByDisplayValue("canonical")).toBeVisible()
    )
    expect(onChange).toHaveBeenLastCalledWith(["canonical"])
  })
})
