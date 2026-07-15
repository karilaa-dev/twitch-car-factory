import { render, screen, within } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { LaunchSource } from "@/components/launch-source"
import type { ChannelSource } from "@/types"

const current: ChannelSource = {
  mode: "preset",
  name: "Evening rotation",
  channels: ["twitchgaming", "monstercat"],
}

describe("LaunchSource", () => {
  it("merges identical current and planned sources", () => {
    render(<LaunchSource current={current} planned={{ ...current }} />)

    expect(screen.getByText("Launch source", { exact: true })).toBeVisible()
    expect(
      screen.queryByLabelText("Launch source diff")
    ).not.toBeInTheDocument()
    expect(screen.getAllByText("Evening rotation")).toHaveLength(1)
    expect(screen.getByText("Source mode")).toBeVisible()
    expect(screen.getByText("Source preset")).toBeVisible()
    expect(screen.getByText("Farming channels")).toBeVisible()
    expect(screen.getByText("preset")).toHaveAttribute(
      "data-variant",
      "secondary"
    )
  })

  it("highlights changed source fields without tinting the comparison card", () => {
    const planned: ChannelSource = {
      mode: "custom",
      name: "primary",
      channels: ["rocketleague"],
    }
    render(<LaunchSource current={current} planned={planned} />)

    const diff = screen.getByLabelText("Launch source diff")
    expect(within(diff).getAllByText("Current").length).toBeGreaterThan(0)
    expect(within(diff).getAllByText("Next").length).toBeGreaterThan(0)
    expect(diff).not.toHaveClass("bg-destructive/10")
    expect(diff).not.toHaveClass("bg-success/10")
    expect(within(diff).getByText("preset")).toHaveAttribute(
      "data-variant",
      "destructive"
    )
    expect(within(diff).getByText("custom")).toHaveAttribute(
      "data-variant",
      "success"
    )
    expect(within(diff).getByText("twitchgaming")).toHaveAttribute(
      "data-variant",
      "destructive"
    )
    expect(within(diff).getByText("rocketleague")).toHaveAttribute(
      "data-variant",
      "success"
    )
    expect(within(diff).getByText("Source reference")).toBeVisible()
    expect(within(diff).getByText("Source preset")).toHaveClass(
      "text-muted-foreground"
    )
    expect(within(diff).getByText("Account override")).toHaveClass(
      "text-muted-foreground"
    )
  })

  it("keeps an unchanged mode and reference neutral when only channels change", () => {
    const planned: ChannelSource = {
      ...current,
      channels: ["twitchgaming", "rocketleague"],
    }
    render(<LaunchSource current={current} planned={planned} />)

    const diff = screen.getByLabelText("Launch source diff")
    expect(within(diff).getByText("preset")).toHaveAttribute(
      "data-variant",
      "secondary"
    )
    expect(within(diff).getAllByText("Evening rotation")).toHaveLength(1)
    expect(within(diff).getByText("monstercat")).toHaveAttribute(
      "data-variant",
      "destructive"
    )
    expect(within(diff).getByText("rocketleague")).toHaveAttribute(
      "data-variant",
      "success"
    )
    for (const unchanged of within(diff).getAllByText("twitchgaming")) {
      expect(unchanged).toHaveAttribute("data-variant", "outline")
    }
  })

  it("uses an ordered diff so an insertion does not mark later channels changed", () => {
    const planned: ChannelSource = {
      ...current,
      channels: ["newchannel", ...current.channels],
    }
    render(<LaunchSource current={current} planned={planned} />)

    const diff = screen.getByLabelText("Launch source diff")
    expect(within(diff).getByText("newchannel")).toHaveAttribute(
      "data-variant",
      "success"
    )
    for (const unchanged of ["twitchgaming", "monstercat"]) {
      for (const badge of within(diff).getAllByText(unchanged)) {
        expect(badge).toHaveAttribute("data-variant", "outline")
      }
    }
  })

  it("compares channel order and ignores source-name casing", () => {
    const { rerender } = render(
      <LaunchSource
        current={current}
        planned={{ ...current, name: "evening ROTATION" }}
      />
    )
    expect(
      screen.queryByLabelText("Launch source diff")
    ).not.toBeInTheDocument()

    rerender(
      <LaunchSource
        current={current}
        planned={{ ...current, channels: [...current.channels].reverse() }}
      />
    )
    expect(screen.getByLabelText("Launch source diff")).toBeVisible()
  })
})
