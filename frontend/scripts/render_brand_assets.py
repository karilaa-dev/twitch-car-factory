"""Render the project SVG logo assets to transparent PNGs."""

from pathlib import Path

from playwright.sync_api import sync_playwright


BRAND_DIR = Path(__file__).resolve().parents[1] / "public" / "brand"
ASSETS = {
    "twitch-farm-mark.svg": (512, 512),
    "twitch-farm-lockup.svg": (1200, 320),
    "twitch-farm-lockup-inverse.svg": (1200, 320),
}


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        for source_name, (width, height) in ASSETS.items():
            source = BRAND_DIR / source_name
            output = source.with_suffix(".png")
            page.set_viewport_size({"width": width, "height": height})
            page.goto(source.as_uri())
            page.locator("svg").screenshot(path=str(output), omit_background=True)
        browser.close()


if __name__ == "__main__":
    main()
