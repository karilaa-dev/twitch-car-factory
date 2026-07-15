"""Verify and capture the production Runtime layout at both breakpoints."""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def assert_no_horizontal_overflow(page: Page, label: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if overflow > 1:
        raise AssertionError(f"{label} overflows horizontally by {overflow}px")


def assert_mobile_touch_targets(page: Page) -> None:
    targets = page.eval_on_selector_all(
        "main button, main [role='link'], header button, header a",
        """
        elements => elements
          .filter(element => element.getClientRects().length > 0)
          .map(element => {
            const rect = element.getBoundingClientRect();
            return {
              height: rect.height,
              name: element.getAttribute('aria-label') || element.textContent || '',
            };
          })
        """,
    )
    undersized = [target for target in targets if target["height"] < 43.5]
    if undersized:
        raise AssertionError(f"Undersized mobile touch targets: {undersized[:6]!r}")


def assert_mobile_navigation_icons(page: Page) -> None:
    for destination in ("Runtime", "Accounts", "Presets", "Logs", "Settings"):
        link = page.locator("header").get_by_role(
            "link", name=destination, exact=True
        )
        icon = link.locator("svg")
        label = link.locator("span")
        assert icon.count() == 1 and icon.is_visible()
        assert icon.bounding_box()["y"] < label.bounding_box()["y"]


def wait_for_runtime(page: Page) -> None:
    page.wait_for_function(
        """
        () => [...document.querySelectorAll('main *')].some(
          element => element.textContent?.trim() === 'demo_running'
            && element.getClientRects().length > 0
        )
        """,
        timeout=15_000,
    )


def assert_selected_layout(page: Page, viewport: str) -> None:
    if viewport == "desktop":
        assert page.get_by_text("Live operations", exact=True).is_visible()
        assert page.get_by_role("heading", name="Needs attention").count() == 0
    else:
        assert page.get_by_role("heading", name="Needs attention").is_visible()
        assert page.get_by_text("Live operations", exact=True).count() == 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="visualadmin")
    parser.add_argument("--password", default="visual-test-password")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/runtime-responsive"),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(f"{args.base_url}/login", wait_until="networkidle")
        page.get_by_label("Username").fill(args.username)
        page.get_by_label("Password").fill(args.password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(f"{args.base_url}/")
        wait_for_runtime(page)

        if page.locator("html.light").count():
            page.get_by_role("button", name="Switch to dark theme").click()

        for viewport, width, height in (
            ("desktop", 1440, 1000),
            ("mobile", 390, 844),
        ):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(f"{args.base_url}/", wait_until="networkidle")
            wait_for_runtime(page)
            assert_selected_layout(page, viewport)
            assert_no_horizontal_overflow(page, viewport)
            if viewport == "mobile":
                assert_mobile_touch_targets(page)
                assert_mobile_navigation_icons(page)
            page.screenshot(
                path=args.output / f"runtime-{viewport}.png",
                full_page=True,
                animations="disabled",
            )

        for viewport, width in (("mobile", 767), ("desktop", 768)):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{args.base_url}/", wait_until="networkidle")
            wait_for_runtime(page)
            assert_selected_layout(page, viewport)
            assert_no_horizontal_overflow(page, f"{width}px breakpoint")
            if viewport == "mobile":
                assert_mobile_navigation_icons(page)

        browser.close()

    ignored = (
        "Failed to load resource: the server responded with a status of 400",
        "The Cross-Origin-Opener-Policy header has been ignored",
    )
    console_errors = [
        error
        for error in console_errors
        if not any(error.startswith(prefix) for prefix in ignored)
    ]
    if console_errors or page_errors:
        raise AssertionError(
            f"Browser errors: console={console_errors!r}, page={page_errors!r}"
        )


if __name__ == "__main__":
    main()
