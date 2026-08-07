#!/usr/bin/env python3
"""Optional Chromium smoke test for the single-file website.

Requires ``playwright`` and a Chromium executable. The test runs entirely in an
``about:blank`` document with an in-page fetch mock, so it performs no network
requests and never modifies the repository's citation data.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
TRANSPARENT_GIF = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def page_content(html_text: str, payload: dict, initial_hash: str) -> str:
    """Inject a deterministic fetch mock before the site's own script executes."""
    offline_html = re.sub(r"<link\b[^>]*>", "", html_text, flags=re.I)
    offline_html = re.sub(
        r'(<img\b[^>]*\bsrc=)["\'][^"\']*["\']',
        lambda match: f'{match.group(1)}"{TRANSPARENT_GIF}"',
        offline_html,
        flags=re.I,
    )
    payload_json = json.dumps(payload).replace("</", "<\\/")
    hash_json = json.dumps(initial_hash)
    bootstrap = f"""
<script>
  history.replaceState({{}}, '', {hash_json});
  window.fetch = async () => new Response(
    JSON.stringify({payload_json}),
    {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }}
  );
</script>
"""
    return offline_html.replace("<body>", "<body>" + bootstrap, 1)


def load_page(page: Page, html_text: str, payload: dict, initial_hash: str = "#publications") -> None:
    page.set_content(page_content(html_text, payload, initial_hash), wait_until="domcontentloaded")
    page.locator("#citation-status").wait_for(state="visible")


def main() -> int:
    chromium = (
        os.environ.get("CHROMIUM_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    if not chromium:
        raise RuntimeError("Chromium executable not found; set CHROMIUM_PATH")

    html_text = (ROOT / "index.html").read_text(encoding="utf-8")
    script_match = re.search(r"<script>\n(.*?)\n</script>", html_text, re.S)
    if not script_match:
        raise RuntimeError("inline script not found")
    inline_script = script_match.group(1)

    valid_data = {
        "first_author": {"1": 101, "17": 17},
        "student": {"s1": 202},
        "coauthor": {"1c": 303, "83": 1},
        "updated": "2026-08-07 06:00 UTC",
        "status": "ok",
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        load_page(page, html_text, valid_data)
        expect(page.url.endswith("#publications"), "deep link hash was not retained")
        expect(page.locator(".hamburger").get_attribute("aria-label") == "Open navigation", "initial mobile label is unclear")
        expect(page.locator("#heroSection").get_attribute("hidden") is not None, "hero should be hidden")
        expect("active" in (page.locator("#publications").get_attribute("class") or ""), "publications tab inactive")
        expect(
            page.locator('.nav-links a[href="#publications"]').get_attribute("aria-current") == "page",
            "active nav item lacks aria-current",
        )

        expected_badges = {
            '[data-citation-section="first_author"] [data-citation-key="1"] .cite-badge': "101 citations",
            '[data-citation-section="student"] [data-citation-key="s1"] .cite-badge': "202 citations",
            '[data-citation-section="coauthor"] [data-citation-key="1c"] .cite-badge': "303 citations",
            '[data-citation-section="coauthor"] [data-citation-key="83"] .cite-badge': "1 citation",
        }
        for selector, expected_text in expected_badges.items():
            locator = page.locator(selector)
            locator.wait_for(state="attached")
            expect(locator.inner_text() == expected_text, f"wrong badge for {selector}")

        page.locator('.nav-links a[href="#research"]').click()
        page.wait_for_function("location.hash === '#research'")
        expect("active" in (page.locator("#research").get_attribute("class") or ""), "research tab inactive")
        page.go_back()
        page.wait_for_function("location.hash === '#publications'")
        expect("active" in (page.locator("#publications").get_attribute("class") or ""), "back navigation failed")

        page.locator(".nav-logo").click()
        page.wait_for_function("location.hash === ''")
        expect(page.locator("#heroSection").get_attribute("hidden") is None, "home did not restore hero")
        expect(page.locator("section.tab-content.active").count() == 0, "home left a tab active")
        page.go_back()
        page.wait_for_function("location.hash === '#publications'")

        page.set_viewport_size({"width": 700, "height": 900})
        hamburger = page.locator(".hamburger")
        hamburger.click()
        expect(hamburger.get_attribute("aria-expanded") == "true", "mobile menu did not open")
        expect(hamburger.inner_text() == "×", "mobile close icon did not update")
        expect(hamburger.get_attribute("aria-label") == "Close navigation", "mobile label did not update")
        expect(page.locator(".nav-links").evaluate("el => getComputedStyle(el).display") == "flex", "mobile menu is hidden")
        page.set_viewport_size({"width": 1200, "height": 900})
        page.wait_for_timeout(100)
        expect(hamburger.get_attribute("aria-expanded") == "false", "resize did not reset menu state")
        expect(page.locator(".nav-links").evaluate("el => getComputedStyle(el).display") == "flex", "desktop nav stayed hidden")

        first_item = page.locator('[data-citation-section="first_author"] .pub-item').first
        first_item.wait_for(state="visible")
        page.wait_for_function(
            "el => el.classList.contains('is-visible')",
            arg=first_item.element_handle(),
        )
        expect("fade-in" in (first_item.get_attribute("class") or ""), "fade-in initialization missing")

        badge_count = page.locator(".cite-badge").count()
        page.add_script_tag(content=inline_script)
        page.wait_for_timeout(300)
        expect(page.locator(".cite-badge").count() == badge_count, "script rerun duplicated citation badges")

        stale_page = context.new_page()
        stale_payload = {
            **valid_data,
            "status": "stale",
            "last_attempt": "2026-08-08 06:00 UTC",
            "missing": ["coauthor:81"],
        }
        load_page(stale_page, html_text, stale_payload)
        stale_status = stale_page.locator("#citation-status")
        expect("last-known NASA ADS values" in stale_status.inner_text(), "stale state not disclosed")
        expect("is-stale" in (stale_status.get_attribute("class") or ""), "stale class missing")

        invalid_page = context.new_page()
        invalid_payload = {
            "first_author": {},
            "student": {},
            "updated": "2026-08-07 06:00 UTC",
            "status": "ok",
        }
        load_page(invalid_page, html_text, invalid_payload)
        invalid_status = invalid_page.locator("#citation-status")
        expect("temporarily unavailable" in invalid_status.inner_text(), "invalid schema was accepted")
        expect("is-error" in (invalid_status.get_attribute("class") or ""), "error class missing")
        expect(invalid_page.locator(".cite-badge").count() == 0, "partial JSON rendered partial badges")

        reduced_context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            reduced_motion="reduce",
        )
        reduced_page = reduced_context.new_page()
        load_page(reduced_page, html_text, valid_data)
        reduced_item = reduced_page.locator('[data-citation-section="first_author"] .pub-item').first
        reduced_classes = reduced_item.get_attribute("class") or ""
        expect("fade-in" in reduced_classes and "is-visible" in reduced_classes, "reduced-motion fallback failed")
        reduced_context.close()

        expect(page_errors == [], f"uncaught browser errors: {page_errors}")
        browser.close()

    print("Chromium smoke test: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
