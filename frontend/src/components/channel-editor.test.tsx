import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { ChannelEditor } from "@/components/channel-editor"
import { api } from "@/lib/api"

vi.mock("@/lib/api", () => ({ api: vi.fn() }))

const apiMock = vi.mocked(api)

describe("ChannelEditor", () => {
  beforeEach(() => {
    apiMock.mockReset()
    apiMock.mockResolvedValue({ name: "alpha", status: "exists" })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it("removes a channel only after confirmation within five seconds", () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    render(
      <ChannelEditor
        id="channels"
        value={["alpha", "beta"]}
        onChange={onChange}
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "Remove alpha" }))

    const confirmButton = screen.getByRole("button", {
      name: "Confirm remove alpha",
    })
    expect(confirmButton).toHaveTextContent("Confirm")
    expect(confirmButton).toHaveClass("min-w-20", "justify-center", "text-xs")
    expect(onChange).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(5_000))

    expect(screen.getByRole("button", { name: "Remove alpha" })).toBeVisible()
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "Remove alpha" }))
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm remove alpha" })
    )

    expect(screen.queryByDisplayValue("alpha")).not.toBeInTheDocument()
    expect(onChange).toHaveBeenLastCalledWith(["beta"])
  })

  it("keeps added channels read-only while leaving a new row editable", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ChannelEditor id="channels" value={["alpha"]} onChange={onChange} />
    )

    const addedChannel = screen.getByDisplayValue("alpha")
    expect(addedChannel).toHaveAttribute("readonly")
    expect(addedChannel).toHaveClass("read-only:text-foreground")
    expect(addedChannel).not.toHaveClass("read-only:text-muted-foreground")
    await user.type(addedChannel, "-changed")
    expect(addedChannel).toHaveValue("alpha")
    expect(onChange).not.toHaveBeenCalled()

    await user.click(screen.getByRole("button", { name: "Add channel" }))
    const newChannel = screen.getAllByPlaceholderText("twitch_channel")[1]
    expect(newChannel).not.toHaveAttribute("readonly")
    await user.type(newChannel, "beta")
    expect(newChannel).toHaveValue("beta")
    expect(onChange).toHaveBeenLastCalledWith(["alpha", "beta"])
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
    render(<ChannelEditor id="channels" value={[]} onChange={onChange} />)
    const input = screen.getByPlaceholderText("twitch_channel")

    await user.type(input, "old_name")

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
    const user = userEvent.setup()
    const onChange = vi.fn()
    apiMock.mockResolvedValueOnce({ name: "canonical", status: "exists" })
    render(<ChannelEditor id="channels" value={[]} onChange={onChange} />)
    const input = screen.getByPlaceholderText("twitch_channel")

    await user.type(input, "Canonical")
    fireEvent.blur(input)

    await waitFor(() =>
      expect(screen.getByDisplayValue("canonical")).toBeVisible()
    )
    expect(screen.getByDisplayValue("canonical")).toHaveAttribute("readonly")
    expect(onChange).toHaveBeenLastCalledWith(["canonical"])
  })

  it("explains priority and numbers rows as an ordered list", () => {
    render(
      <ChannelEditor
        id="channels"
        description="Custom channel guidance."
        value={["alpha", "beta"]}
        onChange={vi.fn()}
      />
    )

    const channels = screen.getByRole("list", {
      name: "Channels ordered by priority",
    })
    expect(channels).toBeVisible()
    expect(within(channels).getAllByRole("listitem")).toHaveLength(2)
    expect(screen.getByText("1")).toBeVisible()
    expect(screen.getByText("2")).toBeVisible()
    expect(
      screen.getByRole("button", {
        name: "Reorder alpha, priority 1 of 2",
      })
    ).toBeVisible()
    expect(
      screen.getByRole("button", {
        name: "Reorder beta, priority 2 of 2",
      })
    ).toBeVisible()
    const guidance = screen.getByRole("list", {
      name: "Channel list guidance",
    })
    expect(guidance).toBeVisible()
    expect(guidance.children).toHaveLength(4)
    expect(guidance.firstElementChild).toHaveTextContent(
      "Priority follows list position; 1 is highest."
    )
    expect(guidance).toHaveTextContent("Custom channel guidance.")
  })

  it("renumbers priorities after adding and removing rows", async () => {
    const user = userEvent.setup()
    render(
      <ChannelEditor
        id="channels"
        value={["alpha", "beta"]}
        onChange={vi.fn()}
      />
    )

    await user.click(screen.getByRole("button", { name: "Add channel" }))
    expect(
      screen.getByRole("button", { name: "Reorder channel, priority 3 of 3" })
    ).toBeVisible()

    await user.click(screen.getByRole("button", { name: "Remove alpha" }))
    await user.click(
      screen.getByRole("button", { name: "Confirm remove alpha" })
    )

    const channels = screen.getByRole("list", {
      name: "Channels ordered by priority",
    })
    expect(within(channels).getAllByRole("listitem")).toHaveLength(2)
    expect(
      screen.getByRole("button", { name: "Reorder beta, priority 1 of 2" })
    ).toBeVisible()
    expect(
      screen.getByRole("button", { name: "Reorder channel, priority 2 of 2" })
    ).toBeVisible()
  })

  it("reorders channel priority with the keyboard", async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        const channel = this.querySelector("input")?.getAttribute("value")
        const top = channel === "alpha" ? 0 : channel === "beta" ? 48 : 96
        return DOMRect.fromRect({ x: 0, y: top, width: 320, height: 40 })
      }
    )
    render(
      <ChannelEditor
        id="channels"
        value={["alpha", "beta", "gamma"]}
        onChange={onChange}
      />
    )

    const alphaHandle = screen.getByRole("button", {
      name: "Reorder alpha, priority 1 of 3",
    })
    alphaHandle.focus()
    await user.keyboard("[Space][ArrowDown][Space]")

    expect(onChange).toHaveBeenLastCalledWith(["beta", "alpha", "gamma"])
    expect(
      screen.getByRole("button", { name: "Reorder beta, priority 1 of 3" })
    ).toBeVisible()
    expect(
      screen.getByRole("button", { name: "Reorder alpha, priority 2 of 3" })
    ).toBeVisible()
  })
})
