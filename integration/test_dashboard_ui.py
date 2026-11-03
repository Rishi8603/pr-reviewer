"""
Browser tests for /dashboard, using Playwright and Chromium.

Most of these replace the API responses using page.route, so they control exactly
what the page receives and need no database. test_dashboard_with_real_data runs
the whole path: browser, API, SQL, PostgreSQL.
"""

import json

import pytest
from playwright.sync_api import expect

EMPTY = {
    "/analytics/summary": {
        "total_repos": 0, "total_prs": 0, "completed_prs": 0, "failed_prs": 0,
        "total_reviews": 0, "total_findings": 0, "critical_findings": 0,
    },
    "/analytics/overview": {"period_days": 30, "repositories": []},
    "/analytics/trends": {"weeks_requested": 12, "trends": []},
    "/analytics/hotspots": {"period_days": 30, "hotspots": []},
}


def _fulfill_with(body: dict):
    def handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


def stub_api(page, responses: dict):
    for path, body in responses.items():
        page.route(f"**{path}", _fulfill_with(body))


def tile_value(page, label: str):
    return page.locator(".tile").filter(has=page.locator(".label", has_text=label)).locator(".value")


def test_empty_state_shows_a_message_instead_of_a_blank_table(page, live_server):
    stub_api(page, EMPTY)
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("#overview .empty")).to_have_text("No reviews in the last 30 days yet.")
    expect(page.locator("#trends .empty")).to_have_text("No completed reviews yet.")
    expect(page.locator("#hotspots .empty")).to_have_text("No findings linked to a file yet.")
    expect(tile_value(page, "PRs reviewed")).to_have_text("0")


def test_api_failure_is_shown_to_the_user(page, live_server):
    stub_api(page, EMPTY)
    page.route("**/analytics/summary", lambda route: route.fulfill(status=503, body="{}"))
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("#summary .error")).to_contain_text("/analytics/summary returned 503")


def test_missing_values_render_as_a_dash(page, live_server):
    stub_api(page, {**EMPTY, "/analytics/overview": {"period_days": 30, "repositories": [
        {"full_name": "acme/alpha", "total_prs_reviewed": 1, "approval_rate_pct": None, "avg_review_seconds": None},
    ]}})
    page.goto(f"{live_server}/dashboard")
    cells = page.locator("#overview td")
    expect(cells.nth(2)).to_have_text("—")
    expect(cells.nth(3)).to_have_text("—")


def test_approval_bar_widths_match_the_counts(page, live_server):
    stub_api(page, {**EMPTY, "/analytics/trends": {"weeks_requested": 12, "trends": [
        {"week": "2026-09-14T00:00:00", "reviews": 4, "approved": 3, "rejected": 1, "avg_duration_sec": 2},
    ]}})
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("#trends .bar .ok")).to_have_attribute("style", "width: 75%;")
    expect(page.locator("#trends .bar .no")).to_have_attribute("style", "width: 25%;")


@pytest.mark.parametrize("payload", [
    "<img src=x onerror=\"window.__xss=1\">",
    "<script>window.__xss=1</script>",
    "\"><svg onload=\"window.__xss=1\">",
])
def test_html_in_a_file_path_is_shown_as_text_and_never_runs(page, live_server, payload):
    # File paths come from PR content and LLM output, so an attacker chooses them.
    stub_api(page, {**EMPTY, "/analytics/hotspots": {"period_days": 30, "hotspots": [
        {"repo": "acme/alpha", "file_path": payload, "total_findings": 1,
         "critical_count": 0, "warning_count": 0, "security_findings": 0},
    ]}})
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("#hotspots td").nth(1)).to_have_text(payload)
    assert page.evaluate("window.__xss") is None
    assert page.locator("#hotspots img, #hotspots script, #hotspots svg").count() == 0


@pytest.mark.parametrize("viewport", [{"width": 375, "height": 812}, {"width": 1280, "height": 800}])
def test_page_does_not_scroll_sideways(page, live_server, viewport):
    page.set_viewport_size(viewport)
    stub_api(page, EMPTY)
    page.goto(f"{live_server}/dashboard")
    expect(page.locator("#overview .empty")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.needs_postgres
def test_dashboard_with_real_data(page, live_server, seeded):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{live_server}/dashboard")

    expect(tile_value(page, "PRs reviewed")).to_have_text("5")
    expect(tile_value(page, "Failed reviews")).to_have_text("1")
    expect(tile_value(page, "Critical findings")).to_have_text("2")
    expect(tile_value(page, "Repositories")).to_have_text("2")

    alpha = page.locator("#overview tr").filter(has_text="acme/alpha")
    expect(alpha).to_contain_text("66.7%")
    expect(alpha).to_contain_text("4 s")
    expect(page.locator("#hotspots tr").nth(1)).to_contain_text("app/a.py")
    assert errors == []
