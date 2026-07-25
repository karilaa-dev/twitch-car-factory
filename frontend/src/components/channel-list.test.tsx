import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  ChannelList,
  RuntimeChannelList,
  WatchedChannelList,
} from "@/components/channel-list"

describe("ChannelList", () => {
  it("separates farming channels into monospace badges and summarizes overflow", () => {
    render(<ChannelList channels={["alpha", "beta", "gamma"]} limit={2} />)

    expect(screen.getByText("alpha")).toHaveClass("font-mono")
    expect(screen.getByText("beta")).toHaveClass("font-mono")
    expect(screen.getByText("+1")).toBeVisible()
    expect(screen.queryByText("gamma")).not.toBeInTheDocument()
  })

  it("marks watched channels with success styling and accessible status", () => {
    render(
      <WatchedChannelList
        channels={["Alpha", "beta"]}
        watchingChannels={[" alpha "]}
      />
    )

    expect(screen.getByLabelText("Alpha (currently watched)")).toHaveAttribute(
      "data-variant",
      "success"
    )
    expect(
      screen.getByLabelText("beta (not currently watched)")
    ).toHaveAttribute("data-variant", "outline")
  })

  it("groups live channels by status without mutating configured order", () => {
    const channels = ["offline", "Online_A", "watched_b", "watched_a"]
    render(
      <RuntimeChannelList
        channels={channels}
        watchingChannels={[" WATCHED_A ", "watched_b"]}
        onlineChannels={["online_a", "watched_b", "watched_a"]}
      />
    )

    expect(
      screen
        .getByLabelText(
          "Farming channels ordered by live status: watched, online, then offline"
        )
        .querySelectorAll("[data-slot='badge']")
    ).toHaveLength(4)
    expect(
      screen
        .getByLabelText(
          "Farming channels ordered by live status: watched, online, then offline"
        )
        .textContent
    ).toBe("watched_bwatched_aOnline_Aoffline")
    expect(
      screen.getByLabelText("watched_b (currently watched)")
    ).toHaveAttribute("data-variant", "success")
    expect(
      screen.getByLabelText("Online_A (online, not currently watched)")
    ).toHaveAttribute("data-variant", "warning")
    expect(
      screen.getByLabelText("offline (offline or status unavailable)")
    ).toHaveAttribute("data-variant", "outline")
    expect(channels).toEqual(["offline", "Online_A", "watched_b", "watched_a"])
  })
})
