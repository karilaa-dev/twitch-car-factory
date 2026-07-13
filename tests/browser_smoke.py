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
        assert page.get_by_role("columnheader", name="Process").count() == 0
        assert "pid 4321" not in page.locator("[data-live-status]").inner_text().lower()
        fitted_channels = page.locator("[data-channel-overflow]").first
        fitted_channels.evaluate("element => { element.style.width = '90px'; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        assert fitted_channels.locator("[data-channel-more]").is_visible()
        assert fitted_channels.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        fitted_channels.evaluate("element => { element.style.width = ''; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        live_panel = page.locator("[data-live-status]").element_handle()
        focused_control = page.locator("[data-live-status] button").first
        focused_handle = focused_control.element_handle()
        focused_control.focus()
        page.wait_for_timeout(5500)
        assert page.evaluate("panel => panel.isConnected", live_panel)
        assert page.evaluate("control => control.isConnected", focused_handle)
        assert focused_control.evaluate("control => control === document.activeElement")
        focused_control.evaluate("control => control.blur()")
        page.screenshot(path=args.output / "dashboard-desktop.png", full_page=True)

        page.locator(".machine-table tbody tr").filter(has_text="running").first.locator(
            ".machine-id a"
        ).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Active launch manifest").is_visible()
        assert page.get_by_text("Exact channels").is_visible()
        source_form = page.locator("[data-channel-source-form]")
        if source_form.count():
            assert source_form.get_by_role("heading", name="Farm default channels").is_visible()
            preset_field = source_form.locator('[data-source-field="preset"]')
            custom_field = source_form.locator('[data-source-field="custom"]')
            assert preset_field.is_hidden()
            assert custom_field.is_hidden()
            source_form.locator('input[name="mode"][value="preset"]').check()
            assert preset_field.is_visible()
            preset_field.locator("select").select_option(index=1)
            assert preset_field.locator("[data-preset-option]:visible").count() == 1
            source_form.locator('input[name="mode"][value="custom"]').check()
            assert custom_field.is_visible()
            assert preset_field.is_hidden()
            custom_editor = custom_field.locator("[data-channel-editor]")
            custom_editor.get_by_label("Add channel").fill("smoke_custom_channel")
            custom_editor.get_by_role("button", name="Add channel").click()
            assert custom_editor.get_by_text("smoke_custom_channel", exact=True).is_visible()
        page.screenshot(path=args.output / "account-detail.png", full_page=True)

        page.get_by_role("link", name="Presets").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Preset library").is_visible()
        assert page.locator(".preset-table tbody tr").count() > 0
        page.screenshot(path=args.output / "presets.png", full_page=True)

        page.locator(".preset-table tbody tr").first.get_by_role("link", name="Open").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Edit preset").is_visible()
        editor = page.locator("[data-channel-editor]")
        editor.get_by_label("Add channel").fill("smoke_staged_channel")
        editor.get_by_role("button", name="Add channel").click()
        assert editor.get_by_text("smoke_staged_channel", exact=True).is_visible()
        editor.get_by_label("Add channel").fill("smoke_staged_channel")
        editor.get_by_role("button", name="Add channel").click()
        assert "already in this list" in editor.locator("[data-channel-feedback]").inner_text()
        staged_row = editor.locator(".channel-editor__row").filter(has_text="smoke_staged_channel")
        staged_row.get_by_role("button", name="Move up").click()
        assert "moved to position" in editor.locator("[data-channel-feedback]").inner_text()
        staged_row = editor.locator(".channel-editor__row").filter(has_text="smoke_staged_channel")
        staged_row.get_by_role("button", name="Remove").click()
        assert editor.get_by_text("smoke_staged_channel", exact=True).count() == 0
        page.screenshot(path=args.output / "preset-editor.png", full_page=True)

        page.get_by_role("link", name="Settings").click()
        page.wait_for_load_state("networkidle")
        settings_editor = page.locator("[data-channel-editor]")
        assert settings_editor.is_visible()
        settings_editor.get_by_label("Add channel").fill("smoke_default_channel")
        settings_editor.get_by_role("button", name="Add channel").click()
        assert settings_editor.get_by_text("smoke_default_channel", exact=True).is_visible()

        page.get_by_role("link", name="Accounts").click()
        page.get_by_role("link", name="Bot logs").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Bot logs").is_visible()
        assert page.locator("[data-log-output]").is_visible()
        page.screenshot(path=args.output / "bot-logs.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{args.base_url}/", wait_until="networkidle")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"Mobile layout overflows horizontally by {overflow}px"
        assert page.locator("[data-channel-overflow]").first.evaluate(
            "element => element.scrollWidth <= element.clientWidth + 1"
        )
        page.screenshot(path=args.output / "dashboard-mobile.png", full_page=True)

        page.set_viewport_size({"width": 320, "height": 720})
        page.goto(f"{args.base_url}/", wait_until="networkidle")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"320px layout overflows horizontally by {overflow}px"

        browser.close()

    if console_errors or page_errors:
        raise AssertionError(
            f"Browser errors: console={console_errors!r}, page={page_errors!r}"
        )


if __name__ == "__main__":
    main()
