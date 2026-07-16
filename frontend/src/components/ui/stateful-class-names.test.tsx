import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Switch } from "@/components/ui/switch"
import { Toggle } from "@/components/ui/toggle"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"

describe("state-aware Base UI classes", () => {
  it("merges consumer callbacks for checkbox, switch, toggle, and button state", async () => {
    const user = userEvent.setup()
    render(
      <>
        <Checkbox
          aria-label="Checkbox"
          defaultChecked
          className={(state) =>
            state.checked ? "consumer-checked" : "consumer-unchecked"
          }
        />
        <Switch
          aria-label="Switch"
          defaultChecked
          className={(state) =>
            state.checked ? "consumer-on" : "consumer-off"
          }
        />
        <Toggle
          aria-label="Toggle"
          defaultPressed
          className={(state) =>
            state.pressed ? "consumer-pressed" : "consumer-released"
          }
        />
        <Button
          disabled
          className={(state) =>
            state.disabled ? "consumer-disabled" : undefined
          }
        >
          Disabled button
        </Button>
      </>
    )

    const checkbox = screen.getByRole("checkbox", { name: "Checkbox" })
    const switchControl = screen.getByRole("switch", { name: "Switch" })
    const toggle = screen.getByRole("button", { name: "Toggle" })
    expect(checkbox).toHaveClass("consumer-checked")
    expect(switchControl).toHaveClass("consumer-on")
    expect(toggle).toHaveClass("consumer-pressed")
    expect(screen.getByRole("button", { name: "Disabled button" })).toHaveClass(
      "consumer-disabled"
    )

    await user.click(checkbox)
    await user.click(switchControl)
    await user.click(toggle)
    expect(checkbox).toHaveClass("consumer-unchecked")
    expect(switchControl).toHaveClass("consumer-off")
    expect(toggle).toHaveClass("consumer-released")
  })

  it("preserves state callbacks on toggle groups and their items", () => {
    render(
      <ToggleGroup
        aria-label="Modes"
        defaultValue={["one"]}
        className={(state) => `consumer-${state.orientation}`}
      >
        <ToggleGroupItem
          value="one"
          className={(state) =>
            state.pressed ? "consumer-selected" : undefined
          }
        >
          One
        </ToggleGroupItem>
      </ToggleGroup>
    )

    expect(screen.getByRole("group", { name: "Modes" })).toHaveClass(
      "consumer-horizontal"
    )
    expect(screen.getByRole("button", { name: "One" })).toHaveClass(
      "consumer-selected"
    )
  })
})
