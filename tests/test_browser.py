"""Browser checks (NFR03, TC12) with Playwright against a live test server.

Skipped automatically when Playwright or its Chromium build is not installed.
"""
import os

import pytest

from .conftest import PASSWORD, component

playwright = pytest.importorskip("playwright.sync_api")
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.browser]


@pytest.fixture
def page(live_server):
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Chromium unavailable: {exc}")
        ctx = browser.new_context(viewport={"width": 360, "height": 780})
        pg = ctx.new_page()
        pg.base = live_server.url
        yield pg
        browser.close()


def login(page, email):
    page.goto(page.base + "/accounts/login/")
    page.fill("#id_contact", email)
    page.fill("#id_password", PASSWORD)
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert "/accounts/login/" not in page.url


def test_TC12_network_interruption_keeps_input_and_retries(page, enrol, version):
    e = enrol()
    login(page, e.participant_user.email)
    page.goto(f"{page.base}/my/{e.pk}/lessons/1/")
    field = page.locator("textarea[name=f__what_is_success]")
    field.fill("Faithfulness in small things")
    form = page.locator("#form-life_work")
    page.context.set_offline(True)
    form.locator("button[value=save]").click()
    state = form.locator(".save-state")
    state.wait_for(state="visible")
    page.wait_for_function("document.querySelector('#form-life_work .save-state').dataset.state === 'error'")
    assert "Not saved" in state.inner_text()
    assert field.input_value() == "Faithfulness in small things"
    assert form.locator(".js-retry").is_visible()
    page.context.set_offline(False)
    form.locator(".js-retry").click()
    page.wait_for_function("document.querySelector('#form-life_work .save-state').dataset.state === 'saved'")
    assert "Draft saved" in state.inner_text()
    page.reload()
    assert page.locator("textarea[name=f__what_is_success]").input_value() == "Faithfulness in small things"
    from apps.learning.models import ComponentResponse

    assert ComponentResponse.objects.get(enrollment=e, component=component(version, 1, "life_work")).revision == 1


def test_validation_errors_identify_field_and_keep_input(page, enrol):
    e = enrol()
    login(page, e.participant_user.email)
    page.goto(f"{page.base}/my/{e.pk}/lessons/1/")
    form = page.locator("#form-case_study")
    form.locator("textarea[name=f__inspiration]").fill("Her compassion")
    form.locator("button[value=submit]").click()
    page.wait_for_selector("#form-case_study .js-error")
    assert form.locator("[data-field=positive_difference] .error-text").inner_text().startswith("This answer is required")
    assert form.locator("textarea[name=f__positive_difference]").get_attribute("aria-invalid") == "true"
    assert form.locator("textarea[name=f__inspiration]").input_value() == "Her compassion"


@pytest.mark.parametrize("path", ["/my/{e}/", "/my/{e}/lessons/3/", "/my/{e}/lessons/12/", "/my/{e}/quiz/"])
def test_NFR03_no_horizontal_overflow_at_360px(page, enrol, path):
    e = enrol()
    login(page, e.participant_user.email)
    page.goto(page.base + path.format(e=e.pk))
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_NFR03_keyboard_only_acknowledge(page, enrol):
    e = enrol()
    login(page, e.participant_user.email)
    page.goto(f"{page.base}/my/{e.pk}/lessons/1/")
    button = page.get_by_role("button", name="I have read this")
    for _ in range(60):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement && document.activeElement.textContent.trim() === 'I have read this'"):
            break
    else:
        pytest.fail("Acknowledge button not reachable by keyboard")
    page.keyboard.press("Enter")
    page.wait_for_load_state()
    assert button.count() == 0 and "marked as read" in page.content()
