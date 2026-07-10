"""Manual Playwright smoke for the rendered control room.

Run this against a prepared local server; it is intentionally not collected by
pytest because installing browser binaries is an environment-level operation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="visualadmin")
    parser.add_argument("--password", default="visual-test-password")
    parser.add_argument("--output", type=Path, default=Path("/private/tmp/twitch-farm-browser"))
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
            lambda message: console_errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(f"{args.base_url}/login/", wait_until="networkidle")
        page.screenshot(path=args.output / "login.png", full_page=True)
        page.get_by_label("Operator ID").fill(args.username)
        page.get_by_label("Passphrase").fill(args.password)
        page.get_by_role("button", name="Enter control room").click()
        page.wait_for_url(f"{args.base_url}/")
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Runtime board").is_visible()
        assert page.get_by_text("Supervisor online").is_visible()
        assert page.locator(".channel-tag").count() > 0
        page.screenshot(path=args.output / "dashboard-desktop.png", full_page=True)

        page.locator(".machine-table tbody tr", has=page.locator(".channel-tag")).first.locator(
            ".machine-id a"
        ).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Active launch manifest").is_visible()
        assert page.get_by_text("Exact channels").is_visible()
        page.screenshot(path=args.output / "account-detail.png", full_page=True)

        page.get_by_role("link", name="Presets").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Preset library").is_visible()
        assert page.locator(".preset-card").count() > 0
        page.screenshot(path=args.output / "presets.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{args.base_url}/", wait_until="networkidle")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"Mobile layout overflows horizontally by {overflow}px"
        page.screenshot(path=args.output / "dashboard-mobile.png", full_page=True)

        browser.close()

    if console_errors or page_errors:
        raise AssertionError(
            f"Browser errors: console={console_errors!r}, page={page_errors!r}"
        )


if __name__ == "__main__":
    main()
