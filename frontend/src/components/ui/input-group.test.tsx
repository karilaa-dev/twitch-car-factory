import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import {
  InputGroup,
  InputGroupAddon,
  InputGroupTextarea,
} from "@/components/ui/input-group"

describe("InputGroupAddon", () => {
  it("focuses an associated textarea", async () => {
    const user = userEvent.setup()
    render(
      <InputGroup>
        <InputGroupAddon>Notes</InputGroupAddon>
        <InputGroupTextarea aria-label="Notes input" />
      </InputGroup>
    )

    await user.click(screen.getByText("Notes"))
    expect(screen.getByRole("textbox", { name: "Notes input" })).toHaveFocus()
  })
})
