import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it } from "vitest"

import { ThemeProvider, useTheme } from "@/components/theme-provider"

function ThemeHarness() {
  const { theme, setTheme } = useTheme()
  return <button onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme}</button>
}

describe("ThemeProvider", () => {
  it("defaults to dark and persists an explicit light selection", async () => {
    localStorage.clear()
    const user = userEvent.setup()
    render(<ThemeProvider defaultTheme="dark" storageKey="test-theme"><ThemeHarness /></ThemeProvider>)
    expect(screen.getByRole("button")).toHaveTextContent("dark")
    await user.click(screen.getByRole("button"))
    expect(localStorage.getItem("test-theme")).toBe("light")
    expect(document.documentElement).toHaveClass("light")
  })
})
