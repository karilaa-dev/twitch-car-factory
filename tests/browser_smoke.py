"""Playwright smoke for the Django-hosted React control room.

Run against a disposable, migrated database seeded with ``seed_demo_data`` and
a staff user. Selectors intentionally follow accessible roles and labels.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def assert_no_horizontal_overflow(page: Page, label: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    if overflow > 1:
        offenders = page.evaluate(
            """
            [...document.querySelectorAll('*')]
              .map((element) => ({ element, rect: element.getBoundingClientRect() }))
              .filter(({ rect }) => rect.right > window.innerWidth + 1 || rect.left < -1)
              .slice(0, 8)
              .map(({ element, rect }) => ({
                tag: element.tagName,
                slot: element.getAttribute('data-slot'),
                className: String(element.className).slice(0, 160),
                text: (element.textContent || '').trim().slice(0, 80),
                left: rect.left,
                right: rect.right,
                width: rect.width,
              }))
            """
        )
        raise AssertionError(
            f"{label} overflows horizontally by {overflow}px: {offenders!r}"
        )


def assert_touch_targets(page: Page, label: str) -> None:
    targets = page.eval_on_selector_all(
        "main button, header button, header a, main [role='link']",
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
    for index, target in enumerate(targets):
        # Tiny icon controls inside dense desktop data views expand to 44px on
        # mobile. Disabled controls still need the same physical hit region.
        assert target["height"] >= 43.5, (
            f"{label} target {index} ({target['name']!r}) "
            f"is only {target['height']}px tall"
        )


def assert_log_run_cards_do_not_overlap(page: Page, label: str) -> None:
    cards = page.locator("[data-log-run-id]").evaluate_all(
        """
        elements => elements.map(element => {
          const rect = element.getBoundingClientRect();
          return {id: element.dataset.logRunId, top: rect.top, bottom: rect.bottom};
        })
        """
    )
    for previous, current in zip(cards, cards[1:], strict=False):
        assert previous["bottom"] <= current["top"] + 1, (
            f"{label} run cards overlap: {previous!r}, {current!r}"
        )


def assert_log_run_cards_are_compact(page: Page, label: str) -> None:
    cards = page.locator("[data-log-run-id]").evaluate_all(
        """
        elements => elements.map(element => ({
          id: element.dataset.logRunId,
          height: element.getBoundingClientRect().height,
        }))
        """
    )
    for card in cards:
        assert 43.5 <= card["height"] <= 56, (
            f"{label} run card {card['id']} is {card['height']}px tall"
        )


def assert_no_browser_errors(
    console_errors: list[str], page_errors: list[str]
) -> None:
    ignored = (
        "Failed to load resource: the server responded with a status of 400",
    )
    filtered = [
        error for error in console_errors if not error.startswith(ignored)
    ]
    if filtered or page_errors:
        raise AssertionError(
            f"Browser errors: console={filtered!r}, page={page_errors!r}"
        )


def run_tv_auth_smoke(page: Page, args, account_id: int) -> None:
    page.goto(
        f"{args.base_url}/accounts/{account_id}?tab=auth",
        wait_until="networkidle",
    )
    page.get_by_role("heading", name="tv_smoke", exact=True).wait_for()
    page.get_by_role("button", name="Connect Twitch", exact=True).click()
    page.get_by_text("FAKE-CODE", exact=True).wait_for(timeout=15_000)
    assert page.get_by_role(
        "link", name="Open twitch.tv/activate", exact=True
    ).get_attribute("href") == "https://www.twitch.tv/activate"
    page.screenshot(path=args.output / "tv-auth-desktop.png", full_page=True)
    for width in (390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        assert_no_horizontal_overflow(page, f"TV auth {width}px")
        assert_tv_auth_targets(page, f"TV auth {width}px")
        page.screenshot(
            path=args.output / f"tv-auth-{width}.png",
            full_page=True,
        )
    page.set_viewport_size({"width": 1440, "height": 1000})


def assert_tv_auth_targets(page: Page, label: str) -> None:
    for target in (
        page.get_by_role("button", name="Copy activation code"),
        page.get_by_role("link", name="Open twitch.tv/activate", exact=True),
    ):
        box = target.bounding_box()
        assert box is not None and box["height"] >= 43.5, (
            f"{label} activation target is too small: {box!r}"
        )


def assert_route(page: Page, base_url: str, route: str, heading: str, label: str) -> None:
    page.goto(f"{base_url}{route}", wait_until="networkidle")
    assert page.get_by_role("heading", name=heading, exact=True).is_visible()
    assert_no_horizontal_overflow(page, label)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="visualadmin")
    parser.add_argument("--password", default="visual-test-password")
    parser.add_argument("--tv-account-id", type=int)
    parser.add_argument("--tv-only", action="store_true")
    parser.add_argument("--logs-filter-only", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("/private/tmp/twitch-farm-browser")
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
        page.screenshot(path=args.output / "login.png", full_page=True)
        try:
            page.get_by_role("heading", name="Twitch Farm Control Room").wait_for(
                timeout=10_000
            )
        except Exception as exc:
            raise AssertionError(
                f"Login did not mount. body={page.locator('body').inner_text()!r}; "
                f"console={console_errors!r}; page={page_errors!r}"
            ) from exc
        page.get_by_label("Username").fill(args.username)
        page.get_by_label("Password").fill(args.password)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(f"{args.base_url}/")
        page.get_by_role("heading", name="Runtime", exact=True).wait_for()
        if args.logs_filter_only:
            page.goto(f"{args.base_url}/logs", wait_until="networkidle")
            page.get_by_role("heading", name="Logs", exact=True).wait_for()
            page.get_by_role(
                "button", name="Show Twitch library logs", exact=True
            ).click()
            page.get_by_text("Twitch library", exact=True).first.wait_for()
            assert_no_horizontal_overflow(page, "Desktop log source filters")
            live_console = page.get_by_label("Live farmer log lines")
            if live_console.count():
                assert "twitch_farm.miner_output" not in live_console.inner_text()
            page.screenshot(
                path=args.output / "logs-filter-desktop.png", full_page=True
            )
            page.get_by_role("tab", name="History").click()
            page.get_by_text("Run archive", exact=True).wait_for()
            assert_log_run_cards_do_not_overlap(page, "Desktop log history")
            assert_log_run_cards_are_compact(page, "Desktop log history")
            archived_console = page.get_by_label("Archived farmer log lines")
            if archived_console.count():
                assert " INFO library account=" not in archived_console.inner_text()
            page.screenshot(
                path=args.output / "logs-history-desktop.png", full_page=True
            )
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                assert_no_horizontal_overflow(page, f"Log source filters {width}px")
                assert_log_run_cards_do_not_overlap(page, f"Log history {width}px")
                assert_log_run_cards_are_compact(page, f"Log history {width}px")
                page.screenshot(
                    path=args.output / f"logs-history-{width}.png", full_page=True
                )
            assert_no_browser_errors(console_errors, page_errors)
            browser.close()
            return
        if args.tv_only:
            if args.tv_account_id is None:
                raise AssertionError("--tv-only requires --tv-account-id")
            run_tv_auth_smoke(page, args, args.tv_account_id)
            assert_no_browser_errors(console_errors, page_errors)
            browser.close()
            return
        assert page.get_by_role("columnheader", name="Current state").count() == 1
        assert page.get_by_role("columnheader", name="Desired").count() == 0
        assert page.get_by_role("columnheader", name="Observed").count() == 0
        intent = page.get_by_role("button", name="Desired state: running").first
        intent.hover()
        page.locator("[data-slot='tooltip-content']").filter(
            has_text="Desired state: running"
        ).wait_for(state="visible")
        page.screenshot(path=args.output / "runtime-desktop.png", full_page=True)

        # Theme state is explicit, persisted, and excludes system mode.
        theme_toggle = page.get_by_role("button", name="Switch to light theme")
        theme_toggle.click()
        assert page.locator("html.light").count() == 1
        assert page.evaluate("localStorage.getItem('twitch-farm-theme')") == "light"
        page.get_by_role("button", name="Switch to dark theme").click()
        assert page.locator("html.dark").count() == 1

        # Polling updates React data without replacing a focused control.
        start_all = page.get_by_role("button", name="Start all", exact=True)
        start_all.focus()
        focused_handle = start_all.element_handle()
        page.wait_for_timeout(5_500)
        assert page.evaluate("element => element.isConnected", focused_handle)
        assert page.evaluate("element => element === document.activeElement", focused_handle)

        # Destructive/global operations use the stock AlertDialog and remain keyboard operable.
        page.get_by_role("button", name="Restart all", exact=True).click()
        dialog = page.get_by_role("alertdialog")
        assert dialog.get_by_role("heading", name="Restart every configured account?").is_visible()
        dialog.get_by_role("button", name="Cancel").press("Enter")
        dialog.wait_for(state="hidden")

        if args.tv_account_id is not None:
            run_tv_auth_smoke(page, args, args.tv_account_id)

        # Every primary route renders from the single React entry point.
        assert_route(page, args.base_url, "/accounts", "Accounts", "Desktop accounts")
        assert page.get_by_role("columnheader", name="Current state").count() == 1
        assert page.get_by_role("columnheader", name="Record").count() == 0
        assert page.get_by_role("columnheader", name="Desired").count() == 0
        assert page.get_by_role("columnheader", name="Observed").count() == 0
        page.screenshot(path=args.output / "accounts-desktop.png", full_page=True)
        account_link = page.locator("main").get_by_role(
            "link", name="Open demo_source_change"
        ).first
        account_link.click()
        page.wait_for_url("**/accounts/*")
        page.get_by_role("heading", name="demo_source_change", exact=True).wait_for()
        assert page.get_by_role("tab", name="Runtime").get_attribute("data-active") is not None
        page.get_by_role("tab", name="History").click()
        assert "tab=history" in page.url
        page.get_by_role("tab", name="Auth settings").click()
        assert "tab=auth" in page.url
        page.get_by_role("tab", name="Runtime").click()
        assert "tab=" not in page.url
        page.get_by_text("Launch source change", exact=True).wait_for()
        source_diff = page.get_by_label("Launch source diff")
        source_diff.wait_for()
        source_diff.get_by_text("Account override", exact=True).wait_for()
        assert (
            source_diff.get_by_text("custom", exact=True).get_attribute("data-variant")
            == "secondary"
        )
        assert (
            source_diff.get_by_text("lirik", exact=True).get_attribute("data-variant")
            == "destructive"
        )
        assert (
            source_diff.get_by_text("twitchgaming", exact=True).get_attribute(
                "data-variant"
            )
            == "success"
        )
        assert page.get_by_text("Current launch", exact=True).count() == 0
        assert page.get_by_text("Planned launch", exact=True).count() == 0
        page.get_by_text("Channel source", exact=True).wait_for()
        page.screenshot(path=args.output / "account-workspace.png", full_page=True)

        page.set_viewport_size({"width": 900, "height": 1000})
        page.goto(f"{args.base_url}/accounts", wait_until="networkidle")
        page.locator("main").get_by_role(
            "link", name="Open demo_preset_content_change"
        ).first.click()
        page.get_by_role(
            "heading", name="demo_preset_content_change", exact=True
        ).wait_for()
        preset_content_diff = page.get_by_label("Launch source diff")
        preset_content_diff.wait_for()
        assert (
            preset_content_diff.get_by_text("preset", exact=True).get_attribute(
                "data-variant"
            )
            == "secondary"
        )
        assert preset_content_diff.get_by_text(
            "Demo evolving rotation", exact=True
        ).count() == 1
        for unchanged_channel in preset_content_diff.get_by_text(
            "lirik", exact=True
        ).all():
            assert unchanged_channel.get_attribute("data-variant") == "outline"
        assert (
            preset_content_diff.get_by_text(
                "cohhcarnage", exact=True
            ).get_attribute("data-variant")
            == "destructive"
        )
        assert (
            preset_content_diff.get_by_text(
                "twitchgaming", exact=True
            ).get_attribute("data-variant")
            == "success"
        )
        preset_content_diff.screenshot(
            path=args.output / "launch-source-preset-content-change.png",
            animations="disabled",
        )

        page.goto(f"{args.base_url}/accounts", wait_until="networkidle")
        page.locator("main").get_by_role(
            "link", name="Open demo_custom_to_preset"
        ).first.click()
        page.get_by_role(
            "heading", name="demo_custom_to_preset", exact=True
        ).wait_for()
        custom_to_preset_diff = page.get_by_label("Launch source diff")
        custom_to_preset_diff.wait_for()
        assert (
            custom_to_preset_diff.get_by_text("custom", exact=True).get_attribute(
                "data-variant"
            )
            == "destructive"
        )
        assert (
            custom_to_preset_diff.get_by_text("preset", exact=True).get_attribute(
                "data-variant"
            )
            == "success"
        )
        custom_to_preset_diff.get_by_text("Account override", exact=True).wait_for()
        custom_to_preset_diff.get_by_text("Source preset", exact=True).wait_for()
        custom_to_preset_diff.screenshot(
            path=args.output / "launch-source-custom-to-preset.png",
            animations="disabled",
        )
        page.set_viewport_size({"width": 1440, "height": 1000})

        assert_route(page, args.base_url, "/presets", "Presets", "Desktop presets")
        page.get_by_role("link", name="Open Demo drops rotation").click()
        page.wait_for_url("**/presets/*")
        page.get_by_role("heading", name="Demo drops rotation", exact=True).wait_for()
        page.get_by_text("Preset configuration", exact=True).wait_for()
        page.get_by_text("Account assignments", exact=True).wait_for()
        assert page.get_by_role("tab", name="Assignments").count() == 0
        page.screenshot(path=args.output / "preset-workspace.png", full_page=True)

        assert_route(page, args.base_url, "/logs", "Logs", "Desktop logs")
        log_viewport = page.locator("[data-slot='scroll-area-viewport']")
        if log_viewport.count():
            log_viewport.evaluate("element => { element.scrollTop = element.scrollHeight }")
            page.wait_for_timeout(5_500)
            distance = log_viewport.evaluate(
                "element => element.scrollHeight - element.scrollTop - element.clientHeight"
            )
            assert distance < 25, f"Log view lost bottom pin by {distance}px"

        assert_route(page, args.base_url, "/settings", "Settings", "Desktop settings")
        page.get_by_role("tab", name="Import").click()
        assert "tab=import" in page.url
        page.get_by_text("Legacy backup import", exact=True).wait_for()

        # Responsive passes exercise the persistent mobile navigation and card layouts.
        for width, height, suffix in ((390, 844, "390"), (320, 720, "320")):
            page.set_viewport_size({"width": width, "height": height})
            assert_route(page, args.base_url, "/", "Runtime", f"{suffix}px runtime")
            assert_touch_targets(page, f"{suffix}px runtime")
            page.screenshot(path=args.output / f"runtime-{suffix}.png", full_page=True)
            if width == 320:
                page.get_by_role(
                    "button", name="Desired state: running"
                ).first.click()
                page.locator("[data-slot='tooltip-content']").filter(
                    has_text="Desired state: running"
                ).wait_for(state="visible")
                page.keyboard.press("Escape")
            nav_boxes = []
            for destination in ("Runtime", "Accounts", "Presets", "Logs", "Settings"):
                nav_link = page.locator("header").get_by_role("link", name=destination, exact=True)
                assert nav_link.is_visible()
                nav_boxes.append(nav_link.bounding_box())
            assert max(box["y"] for box in nav_boxes) - min(box["y"] for box in nav_boxes) < 1
            if width == 390:
                page.locator("main").get_by_role("link", name="Open demo_running").click(position={"x": 20, "y": 20})
                page.wait_for_url("**/accounts/*")
                page.get_by_text("Launch source", exact=True).wait_for()
                assert_no_horizontal_overflow(page, "390px runtime card destination")
                page.goto(f"{args.base_url}/", wait_until="networkidle")
            page.locator("header").get_by_role("link", name="Accounts", exact=True).press("Enter")
            page.wait_for_url(f"{args.base_url}/accounts")
            page.get_by_role("heading", name="Accounts", exact=True).wait_for()
            assert_no_horizontal_overflow(page, f"{suffix}px accounts")
            assert_touch_targets(page, f"{suffix}px accounts")
            page.screenshot(path=args.output / f"accounts-{suffix}.png", full_page=True)
            detail_account = (
                "demo_source_change" if width == 390 else "demo_running"
            )
            page.locator("main").get_by_role(
                "link", name=f"Open {detail_account}"
            ).click(position={"x": 20, "y": 20})
            if width == 390:
                page.get_by_text("Launch source change", exact=True).wait_for()
                page.get_by_label("Launch source diff").wait_for()
            else:
                page.get_by_text("Launch source", exact=True).wait_for()
            assert_no_horizontal_overflow(page, f"{suffix}px account workspace")
            assert_touch_targets(page, f"{suffix}px account workspace")
            page.screenshot(path=args.output / f"account-workspace-{suffix}.png", full_page=True)

            for route, heading in (
                ("/presets", "Presets"),
                ("/logs", "Logs"),
                ("/settings", "Settings"),
            ):
                assert_route(page, args.base_url, route, heading, f"{suffix}px {heading}")
                assert_touch_targets(page, f"{suffix}px {heading}")

            if width == 390:
                page.goto(f"{args.base_url}/presets", wait_until="networkidle")
                page.get_by_role("link", name="Open Demo drops rotation").click()
                page.get_by_role("tab", name="Assignments").click()
                assert "tab=assignments" in page.url
                page.get_by_text("Account assignments", exact=True).wait_for()
                assert_no_horizontal_overflow(page, "390px preset workspace")
                assert_touch_targets(page, "390px preset workspace")
                page.screenshot(path=args.output / "preset-workspace-390.png", full_page=True)

        browser.close()

    assert_no_browser_errors(console_errors, page_errors)


if __name__ == "__main__":
    main()
