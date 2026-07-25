import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

describe("DropdownMenu", () => {
  it("renders a labeled operator group without crashing", async () => {
    render(
      <DropdownMenu defaultOpen>
        <DropdownMenuTrigger>Operator</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuGroup>
            <DropdownMenuLabel>Operator</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem>Log out</DropdownMenuItem>
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    )

    expect(await screen.findByRole("menu")).toBeVisible()
    expect(screen.getByRole("menuitem", { name: "Log out" })).toBeVisible()
    expect(screen.getByText("Operator", { selector: "div" })).toBeVisible()
  })
})
