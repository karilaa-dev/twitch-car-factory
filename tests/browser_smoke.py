"""Manual Playwright smoke for the rendered control room.

Run this against a prepared local server; it is intentionally not collected by
pytest because installing browser binaries is an environment-level operation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

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

        def validate_channel(route):
            name = parse_qs(urlparse(route.request.url).query).get("name", [""])[0]
            if name == "missing_smoke_channel":
                status = "missing"
            elif name == "unverified_smoke_channel":
                status = "unverified"
            else:
                status = "exists"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"name": name, "status": status}),
            )

        page.route(re.compile(r".*/channels/validate/\?name=.*"), validate_channel)

        page.goto(f"{args.base_url}/login/", wait_until="networkidle")
        page.screenshot(path=args.output / "login.png", full_page=True)
        page.get_by_label("Operator ID").fill(args.username)
        page.get_by_label("Passphrase").fill(args.password)
        page.get_by_role("button", name="Enter control room").click()
        page.wait_for_url(f"{args.base_url}/")
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Runtime board").is_visible()
        assert page.locator(".page-header .lede").count() == 0
        assert page.get_by_text("Degraded / open incidents").is_visible()
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
        live_status = page.locator("[data-live-status]")
        stop_button = live_status.get_by_role("button", name="Stop", exact=True).first
        start_button = live_status.get_by_role("button", name="Start", exact=True).first
        assert "button--danger" in stop_button.get_attribute("class").split()
        assert "button--quiet" not in stop_button.get_attribute("class").split()
        assert "button--safe" in start_button.get_attribute("class").split()
        assert "button--quiet" not in start_button.get_attribute("class").split()
        page.screenshot(path=args.output / "dashboard-desktop.png", full_page=True)

        page.locator(".machine-table tbody tr").filter(has_text="Default").first.locator(
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
            custom_editor.get_by_text("smoke_custom_channel", exact=True).wait_for()
        page.screenshot(path=args.output / "account-detail.png", full_page=True)

        page.get_by_role("link", name="Presets").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Preset library").is_visible()
        assert page.locator(".page-header .lede").count() == 0
        assert page.locator(".preset-table tbody tr").count() > 0
        page.set_viewport_size({"width": 900, "height": 900})
        preset_wrap = page.locator(".preset-table").locator("xpath=..")
        assert preset_wrap.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        preset_channels = page.locator(".preset-table [data-channel-overflow]").first
        preset_channels.evaluate("element => { element.style.width = '90px'; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        assert preset_channels.locator("[data-channel-more]").is_visible()
        assert preset_channels.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        preset_channels.evaluate("element => { element.style.width = ''; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        page.screenshot(path=args.output / "presets.png", full_page=True)

        preset_row = page.locator(".preset-table tbody tr").first
        presets_url = page.url
        preset_row.click(button="middle")
        preset_row.click(modifiers=["Meta"])
        assert page.url == presets_url
        preset_row.click(position={"x": 4, "y": 4})
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Edit preset").is_visible()
        page.go_back(wait_until="networkidle")
        page.locator(".preset-table tbody tr").first.locator(
            'td[data-label="Assignments"] .status-chip'
        ).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Edit preset").is_visible()
        editor = page.locator("[data-channel-editor]")
        editor.get_by_label("Add channel").fill("smoke_staged_channel")
        editor.get_by_role("button", name="Add channel").click()
        editor.get_by_text("smoke_staged_channel", exact=True).wait_for()
        editor.get_by_label("Add channel").fill("smoke_staged_channel")
        editor.get_by_role("button", name="Add channel").click()
        assert "already in this list" in editor.locator("[data-channel-feedback]").inner_text()
        staged_row = editor.locator(".channel-editor__row").filter(has_text="smoke_staged_channel")
        assert editor.get_by_role("button", name=re.compile(r"Move (up|down)")).count() == 0
        drag_handle = staged_row.get_by_role(
            "button", name=re.compile(r"^Drag smoke_staged_channel")
        )
        drag_handle.drag_to(
            editor.locator(".channel-editor__row").first,
            target_position={"x": 20, "y": 2},
        )
        assert "moved to position" in editor.locator("[data-channel-feedback]").inner_text()
        assert editor.locator(".channel-editor__row").first.get_by_text(
            "smoke_staged_channel", exact=True
        ).is_visible()
        page.screenshot(path=args.output / "preset-drag.png", full_page=True)
        staged_row = editor.locator(".channel-editor__row").filter(
            has_text="smoke_staged_channel"
        )
        staged_row.get_by_role(
            "button", name=re.compile(r"^Drag smoke_staged_channel")
        ).press("ArrowDown")
        assert "moved to position" in editor.locator("[data-channel-feedback]").inner_text()
        staged_row = editor.locator(".channel-editor__row").filter(has_text="smoke_staged_channel")
        staged_row.get_by_role("button", name="Remove").click()
        assert editor.get_by_text("smoke_staged_channel", exact=True).count() == 0
        staged_rows = editor.locator(".channel-editor__row")
        for _ in range(staged_rows.count()):
            staged_rows.first.get_by_role(
                "button", name="Remove"
            ).click()
        assert staged_rows.count() == 0
        page.get_by_role("button", name="Save preset").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("This field is required.").first.is_visible()
        expected_validation_error = (
            "Failed to load resource: the server responded with a status of 400 (Bad Request)"
        )
        if expected_validation_error in console_errors:
            console_errors.remove(expected_validation_error)
        page.screenshot(path=args.output / "preset-editor.png", full_page=True)

        page.get_by_role("link", name="Settings").click()
        page.wait_for_load_state("networkidle")
        assert page.locator(".page-header .lede").count() == 0
        assert page.get_by_text("What these settings control").count() == 0
        assert page.get_by_text("Database authority").count() == 0
        settings_editor = page.locator("[data-channel-editor]")
        assert settings_editor.is_visible()
        settings_editor.get_by_label("Add channel").fill("smoke_default_channel")
        settings_editor.get_by_role("button", name="Add channel").click()
        settings_editor.get_by_text("smoke_default_channel", exact=True).wait_for()

        page.get_by_label("Primary navigation").get_by_role("link", name="Accounts").click()
        page.wait_for_load_state("networkidle")
        assert page.locator(".page-header .lede").count() == 0
        account_wrap = page.locator(".account-table").locator("xpath=..")
        assert account_wrap.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        account_channels = page.locator(".account-table [data-channel-overflow]").first
        account_channels.evaluate("element => { element.style.width = '90px'; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        assert account_channels.locator("[data-channel-more]").is_visible()
        assert account_channels.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        account_channels.evaluate("element => { element.style.width = ''; }")
        page.evaluate("window.controlRoomFitChannels(document)")
        page.screenshot(path=args.output / "accounts.png", full_page=True)
        account_row = page.locator(".account-table tbody tr").filter(
            has_text="demo-running"
        )
        account_row.click(position={"x": 4, "y": 4})
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Active launch manifest").is_visible()
        page.go_back(wait_until="networkidle")
        page.locator(".account-table tbody tr").filter(has_text="demo-running").locator(
            'td[data-label="Current status"] .status-chip'
        ).click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Active launch manifest").is_visible()
        page.get_by_label("Primary navigation").get_by_role("link", name="Accounts").click()
        page.wait_for_load_state("networkidle")
        page.get_by_role("link", name="Bot logs").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="Bot logs").is_visible()
        assert (
            page.locator("[data-log-output]").is_visible()
            or page.locator("[data-log-empty]").is_visible()
        )
        page.screenshot(path=args.output / "bot-logs.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(f"{args.base_url}/", wait_until="networkidle")
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"Mobile layout overflows horizontally by {overflow}px"
        assert page.locator("[data-channel-overflow]").first.evaluate(
            "element => element.scrollWidth <= element.clientWidth + 1"
        )
        page.screenshot(path=args.output / "dashboard-mobile.png", full_page=True)

        page.goto(f"{args.base_url}/accounts/", wait_until="networkidle")
        mobile_status = page.locator(
            '.account-table td[data-label="Current status"] .status-chip'
        ).first
        assert mobile_status.evaluate(
            "element => element.getBoundingClientRect().width < "
            "element.parentElement.getBoundingClientRect().width - 8"
        )

        page.goto(f"{args.base_url}/presets/", wait_until="networkidle")
        mobile_assignment = page.locator(
            '.preset-table td[data-label="Assignments"] .status-chip'
        ).first
        assert mobile_assignment.evaluate(
            "element => element.getBoundingClientRect().width < "
            "element.parentElement.getBoundingClientRect().width - 8"
        )

        page.get_by_role("link", name="Create preset").click()
        page.wait_for_load_state("networkidle")
        assert page.get_by_text("Reliability contract").count() == 0
        assert page.locator(".detail-layout--single").count() == 1
        new_editor = page.locator("[data-channel-editor]")
        new_editor.get_by_label("Add channel").fill("missing_smoke_channel")
        new_editor.get_by_role("button", name="Add channel").click()
        new_editor.get_by_text("missing_smoke_channel does not exist on Twitch.").wait_for()
        assert new_editor.locator(".channel-editor__row").count() == 0
        new_editor.get_by_label("Add channel").fill("unverified_smoke_channel")
        new_editor.get_by_role("button", name="Add channel").click()
        new_editor.get_by_text(
            "unverified_smoke_channel added, but Twitch could not verify it right now."
        ).wait_for()
        new_editor.get_by_text("unverified_smoke_channel", exact=True).wait_for()
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, f"Mobile channel editor overflows by {overflow}px"

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
