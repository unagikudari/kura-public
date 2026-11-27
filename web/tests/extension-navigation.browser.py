"""Verify that a built Web header matches the composed extension set."""
import importlib.util
import json
from pathlib import Path

from playwright.sync_api import expect


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fixture", HERE / "compact-tag-filter.browser.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
state = HERE.parents[1] / ".kura-extensions.json"
installed = {entry["id"] for entry in json.loads(state.read_text())["extensions"]} if state.exists() else set()


def exercise(context, base, width, phone, evidence):
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/m/client-timing", lambda route: route.fulfill(status=204))
    page.goto(base + "/?q=", wait_until="networkidle")
    nav = page.locator("nav.primary-nav")
    extensions = (
        "lora",
    )
    for extension in extensions:
        link = nav.locator(f'a[href="/{extension}"]')
        if extension in installed:
            expect(link).to_have_count(1)
            expect(link).to_be_in_viewport(ratio=.98)
        else:
            expect(link).to_have_count(0)
    assert not errors, errors
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.close()
    return {"width": width, "installed_extensions": sorted(installed)}


fixture.exercise = exercise
if __name__ == "__main__":
    fixture.main()
