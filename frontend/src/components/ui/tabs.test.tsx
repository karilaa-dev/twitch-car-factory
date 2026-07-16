import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

describe("Tabs", () => {
  it("forwards vertical orientation to Base UI keyboard behavior", async () => {
    const user = userEvent.setup()
    const { container } = render(
      <Tabs defaultValue="one" orientation="vertical">
        <TabsList aria-label="Sections">
          <TabsTrigger value="one">One</TabsTrigger>
          <TabsTrigger value="two">Two</TabsTrigger>
        </TabsList>
        <TabsContent value="one">First panel</TabsContent>
        <TabsContent value="two">Second panel</TabsContent>
      </Tabs>
    )
    const first = screen.getByRole("tab", { name: "One" })
    first.focus()

    await user.keyboard("{ArrowDown}")

    expect(screen.getByRole("tab", { name: "Two" })).toHaveFocus()
    expect(container.querySelector("[data-slot='tabs']")).toHaveAttribute(
      "data-orientation",
      "vertical"
    )
  })
})
