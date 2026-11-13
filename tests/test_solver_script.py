"""Unit tests for the browser-side solving core (no real Chrome needed)."""

import json
import shutil
import subprocess

import pytest

from solver.browser import (
    CLICK_WIDGET_JS,
    DIAGNOSTICS_JS,
    READ_TOKEN_JS,
    TURNSTILE_SCRIPT_URL,
    SolveFailed,
    TurnstileSolver,
    build_injection_script,
)

NODE = shutil.which("node")


def test_script_contains_widget_params():
    js = build_injection_script("0xKEY", theme="light", size="compact", appearance="always")
    assert "0xKEY" in js
    assert "light" in js
    assert "compact" in js
    assert "__solverTsToken" in js


def test_script_defaults_action_and_cdata_to_null():
    js = build_injection_script("0xKEY")
    assert "var action = null;" in js
    assert "var cData = null;" in js


def test_script_passes_action_and_cdata_when_given():
    js = build_injection_script(
        "0xKEY", action="login", c_data="eyJpZCI6MX0="
    )
    assert '"login"' in js
    assert "eyJpZCI6MX0=" in js
    # action is conditionally merged into the render options.
    assert "if (action) { options.action = action; }" in js
    assert "if (cData) { options.cData = cData; }" in js


def test_script_includes_turnstile_script_url():
    assert TURNSTILE_SCRIPT_URL in build_injection_script("0xKEY")


def test_script_escapes_sitekey():
    js = build_injection_script('"><script>alert(1)</script>')
    assert '"><script>' not in js
    assert '\\"' in js


def test_script_escapes_action():
    js = build_injection_script("0xKEY", action='"><script>alert(1)</script>')
    assert '"><script>' not in js


def test_read_token_js_mentions_input():
    assert "cf-turnstile-response" in READ_TOKEN_JS


def test_scripts_are_not_iife_wrapped():
    for script in (READ_TOKEN_JS, CLICK_WIDGET_JS, DIAGNOSTICS_JS):
        assert not script.lstrip().startswith("(function")
    assert not build_injection_script("0xKEY").lstrip().startswith("(function")


def test_module_does_not_import_the_optional_browser_library():
    """The invalid turnstile-solver/pyautogui stack must be gone."""
    import inspect

    from solver import browser

    source = inspect.getsource(browser)
    assert "turnstile_solver" not in source
    assert "pyautogui" not in source


@pytest.mark.skipif(NODE is None, reason="node is needed to emulate Selenium's function body")
def test_scripts_return_a_value_from_a_function_body():
    """Selenium wraps scripts in a function body, so an IIFE's value is lost."""
    harness = r"""
const fs = require("fs");
const scripts = JSON.parse(fs.readFileSync(0, "utf8"));
const wrapper = {
  getBoundingClientRect: () => ({left: 10, top: 20, width: 300, height: 65}),
};
const input = {value: "input-token", parentElement: wrapper};
const container = {
  firstElementChild: wrapper,
  querySelector: (sel) => (sel.includes("cf-turnstile-response") ? input : null),
};
globalThis.window = {__solverTsToken: "", __solverTsError: "", __solverTsWidgetId: "w1"};
globalThis.document = {
  title: "SOLVER",
  readyState: "complete",
  body: {innerText: "hello"},
  getElementById: (id) => (id === "__solver-turnstile" ? container : null),
  querySelector: (sel) => (sel.includes("cf-turnstile-response") ? input : null),
  querySelectorAll: () => [1],
  elementFromPoint: () => ({tagName: "DIV"}),
};
globalThis.location = {href: "https://example.com/login"};
console.log(JSON.stringify(scripts.map((script) => new Function(script)())));
"""
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps([READ_TOKEN_JS, CLICK_WIDGET_JS, DIAGNOSTICS_JS]),
        capture_output=True,
        text=True,
        check=True,
    )
    token, click, diagnostics = json.loads(result.stdout)

    assert token == "input-token"
    # The challenge iframe lives in a closed shadow root, so the click target
    # has to be derived from the light-DOM wrapper.
    assert click["x"] == 40.0 and click["y"] == 52.0
    assert click["hit"] == "DIV"
    assert diagnostics["wrapper"] is True
    assert diagnostics["widgetRect"] == [10, 20, 300, 65]
    assert diagnostics["inputLen"] == len("input-token")
    assert diagnostics["pageText"] == "hello"
    assert diagnostics["url"] == "https://example.com/login"


def test_start_driver_passes_chrome_args(monkeypatch):
    import sys
    import types

    captured = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("seleniumbase")
    fake_module.Driver = FakeDriver
    monkeypatch.setitem(sys.modules, "seleniumbase", fake_module)
    monkeypatch.setenv("CHROME_ARGS", "--disable-dev-shm-usage,--window-size=1280,900")

    solver = TurnstileSolver(headless=False)
    solver._start_driver()

    assert captured["uc"] is True
    assert captured["headless"] is False
    assert captured["chromium_arg"] == "--disable-dev-shm-usage,--window-size=1280,900"


def test_start_driver_without_chrome_args(monkeypatch):
    import sys
    import types

    captured = {}

    class FakeDriver:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.ModuleType("seleniumbase")
    fake_module.Driver = FakeDriver
    monkeypatch.setitem(sys.modules, "seleniumbase", fake_module)
    monkeypatch.delenv("CHROME_ARGS", raising=False)

    TurnstileSolver()._start_driver()

    assert captured == {"uc": True, "headless": False}


def test_get_token_rejects_empty_result():
    solver = TurnstileSolver.__new__(TurnstileSolver)
    with pytest.raises(SolveFailed) as ei:
        solver._validate("")
    assert ei.value.stage == "no_token"


def test_get_token_bounds_browser_work_and_reads_token():
    class FakeDriver:
        def __init__(self):
            self.calls = []

        def set_page_load_timeout(self, value):
            self.calls.append(("page_load_timeout", value))

        def set_script_timeout(self, value):
            self.calls.append(("script_timeout", value))

        def get(self, url):
            self.calls.append(("get", url))

        def execute_script(self, script):
            if "cf-turnstile-response" in script:
                return "token-from-input"
            if "navigator.userAgent" in script:
                return "FakeUA"
            return "loading"

    solver = TurnstileSolver(timeout=7.5)
    solver._driver = FakeDriver()
    result = solver.get_token("https://example.com/login", "0xKEY")

    assert ("page_load_timeout", 7.5) in solver._driver.calls
    assert ("script_timeout", 7.5) in solver._driver.calls
    assert result.token == "token-from-input"
    assert result.user_agent == "FakeUA"


def test_execute_mode_kicks_turnstile_execute_after_widget_render():
    order = []

    class FakeDriver:
        def set_page_load_timeout(self, value):
            pass

        def set_script_timeout(self, value):
            pass

        def get(self, url):
            pass

        def execute_script(self, script):
            if "cf-turnstile-response" in script:
                return "" if not order else "execute-token"
            if "navigator.userAgent" in script:
                return "FakeUA"
            if "window.__solverTsWidgetId" in script:
                return "w1" if order else ""
            return "rendered"

        def quit(self):
            pass

        def execute_cdp_cmd(self, command, params):
            pass

    solver = TurnstileSolver(timeout=10.0, poll=0.01)
    solver._driver = FakeDriver()
    solver._kick_execute = lambda driver: order.append("execute") or "requested"

    result = solver.get_token(
        "https://example.com/login", "0xKEY", appearance="execute", execute=True
    )

    assert "execute" in order[:1]
    assert result.token == "execute-token"


def test_no_token_failure_includes_diagnostics():
    class FakeDriver:
        def set_page_load_timeout(self, value):
            pass

        def set_script_timeout(self, value):
            pass

        def get(self, url):
            pass

        def execute_script(self, script):
            if "window.__solverTsError" in script:
                return {
                    "turnstile": "object",
                    "container": True,
                    "jsError": "300030",
                    "inputs": 1,
                }
            if "cf-turnstile-response" in script:
                return ""
            if "navigator.userAgent" in script:
                return "FakeUA"
            return "rendered"

        def quit(self):
            pass

    solver = TurnstileSolver(timeout=0.05, poll=0.01)
    solver._driver = FakeDriver()
    solver._click_injected_widget = lambda driver: True

    with pytest.raises(SolveFailed) as ei:
        solver.get_token("https://example.com/login", "0xKEY")
    assert ei.value.stage == "no_token"
    assert "diagnostics" in ei.value.message
    assert "300030" in ei.value.message


def test_click_injected_widget_uses_cdp_events():
    class FakeDriver:
        def __init__(self):
            self.cdp = []

        def execute_script(self, script):
            if "getBoundingClientRect" in script:
                return {"x": 40.0, "y": 500.0, "hit": "DIV", "rect": [10, 480, 300, 65]}
            return None

        def execute_cdp_cmd(self, command, params):
            self.cdp.append((command, params))

    driver = FakeDriver()
    solver = TurnstileSolver.__new__(TurnstileSolver)

    assert solver._click_injected_widget(driver) is True
    mouse = [params for command, params in driver.cdp
             if command == "Input.dispatchMouseEvent"]
    assert [params["type"] for params in mouse] == [
        "mouseMoved",
        "mouseMoved",
        "mouseMoved",
        "mouseMoved",
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]
    assert mouse[-2]["button"] == "left"
    assert mouse[-2]["x"] == 40.0 and mouse[-2]["y"] == 500.0
    assert mouse[-1]["button"] == "left"


def test_click_skips_when_widget_is_not_found():
    class FakeDriver:
        def execute_script(self, script):
            return {"error": "no-wrapper"}

    solver = TurnstileSolver.__new__(TurnstileSolver)
    assert solver._click_injected_widget(FakeDriver()) is False