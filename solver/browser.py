"""Turnstile solving inside a real browser (site agnostic).

A widget is rendered on the *target site itself* (``url``) with the caller's
``sitekey``.  Rendering on the target origin is what keeps the resulting token
bound to the correct hostname; that is why the API takes a ``url`` instead of
solving on a neutral page.

The approach is deliberately dependency-light: ``seleniumbase`` (Chrome, UC
mode) plus the Chrome DevTools Protocol.  There is no ``turnstile-solver`` /
PyAutoGUI / OpenCV dependency - the interactive checkbox is clicked straight
through CDP on the widget's light-DOM wrapper (Turnstile keeps the challenge
iframe inside a *closed* shadow root, so ``querySelector("iframe")`` never
sees it).

``seleniumbase`` is imported lazily so the pure string helpers below can be
unit-tested without a browser stack.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

TURNSTILE_SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"

READ_TOKEN_JS = """
if (window.__solverTsToken) { return window.__solverTsToken; }
var input = document.querySelector('input[name="cf-turnstile-response"]');
return input && input.value ? input.value : "";
"""

CLICK_WIDGET_JS = """
var container = document.getElementById("__solver-turnstile");
if (!container) { return {error: "no-container"}; }
var input = container.querySelector('input[name="cf-turnstile-response"]');
var wrapper = input ? input.parentElement : container.firstElementChild;
if (!wrapper) { return {error: "no-wrapper"}; }
var rect = wrapper.getBoundingClientRect();
if (rect.width < 10 || rect.height < 10) {
  var host = wrapper.firstElementChild;
  var hostRect = host ? host.getBoundingClientRect() : null;
  if (hostRect && hostRect.height >= 10) { rect = hostRect; }
}
if (rect.width < 10 || rect.height < 10) {
  return {error: "tiny-widget", rect: [rect.left, rect.top, rect.width, rect.height]};
}
var x = rect.left + Math.min(30, rect.width / 2);
var y = rect.top + Math.min(32, rect.height / 2);
var hit = document.elementFromPoint(x, y);
return {
  x: x,
  y: y,
  hit: hit ? String(hit.tagName) : "none",
  rect: [Math.round(rect.left), Math.round(rect.top),
         Math.round(rect.width), Math.round(rect.height)]
};
"""

EXECUTE_WIDGET_JS = """
if (!window.turnstile || typeof window.turnstile.execute !== "function") {
  return "no-api";
}
var id = window.__solverTsWidgetId;
if (!id) { return "no-widget"; }
try {
  var pending = window.turnstile.execute(id);
  if (pending && typeof pending.catch === "function") { pending.catch(function () {}); }
  return "requested";
} catch (err) {
  return "error:" + String(err);
}
"""

DIAGNOSTICS_JS = """
var container = document.getElementById("__solver-turnstile");
var input = container ? container.querySelector('input[name="cf-turnstile-response"]') : null;
if (!input) { input = document.querySelector('input[name="cf-turnstile-response"]'); }
var wrapper = input ? input.parentElement
  : (container ? container.firstElementChild : null);
var rect = wrapper ? wrapper.getBoundingClientRect() : null;
return {
  url: String(location.href),
  title: String(document.title),
  readyState: String(document.readyState),
  api: typeof window.turnstile,
  stage: String(window.__solverTsStage || ""),
  scriptState: String(window.__solverTsScript || ""),
  csp: String(window.__solverTsCsp || ""),
  widgetId: String(window.__solverTsWidgetId || ""),
  jsError: String(window.__solverTsError || ""),
  tokenLen: String(window.__solverTsToken || "").length,
  container: !!container,
  wrapper: !!wrapper,
  widgetRect: rect ? [Math.round(rect.left), Math.round(rect.top),
                      Math.round(rect.width), Math.round(rect.height)] : null,
  inputs: document.querySelectorAll('input[name="cf-turnstile-response"]').length,
  inputLen: input && input.value ? input.value.length : 0,
  pageText: String((document.body && document.body.innerText) || "").slice(0, 120)
};
"""

_CONTAINER_ID = "__solver-turnstile"

log = logging.getLogger("solver")

_INJECTION_TEMPLATE = """
  var sitekey = %(sitekey)s;
  var theme = %(theme)s;
  var size = %(size)s;
  var appearance = %(appearance)s;
  var action = %(action)s;
  var cData = %(c_data)s;
  var scriptUrl = %(script_url)s;
  var containerId = %(container_id)s;

  window.__solverTsToken = "";
  window.__solverTsError = "";
  window.__solverTsStage = "start";
  window.__solverTsScript = "none";
  window.__solverTsWidgetId = "";

  if (!window.__solverTsCsp) {
    window.__solverTsCsp = "";
    document.addEventListener("securitypolicyviolation", function (e) {
      window.__solverTsCsp = String(e.violatedDirective) + " " + String(e.blockedURI);
    }, true);
  }

  function ensureContainer() {
    var el = document.getElementById(containerId);
    if (!el) {
      el = document.createElement("div");
      el.id = containerId;
      el.style.position = "fixed";
      el.style.top = "120px";
      el.style.left = "20px";
      el.style.width = "320px";
      el.style.height = "70px";
      el.style.zIndex = "2147483647";
      (document.body || document.documentElement).appendChild(el);
    }
    return el;
  }

  function renderWidget() {
    if (!window.turnstile || typeof window.turnstile.render !== "function") {
      if (window.__solverTsStage !== "rendered") { window.__solverTsStage = "waiting-api"; }
      return false;
    }
    try {
      var options = {
        sitekey: sitekey,
        theme: theme,
        size: size,
        appearance: appearance,
        retry: "auto",
        "refresh-expired": "auto",
        callback: function (token) {
          window.__solverTsToken = token || "";
          window.__solverTsStage = "solved";
        },
        "error-callback": function (code) {
          window.__solverTsError = String(code);
          window.__solverTsStage = "error:" + String(code);
        },
        "expired-callback": function () { window.__solverTsToken = ""; },
        "timeout-callback": function () {
          window.__solverTsError = "timeout";
          window.__solverTsStage = "timeout";
        }
      };
      if (action) { options.action = action; }
      if (cData) { options.cData = cData; }
      var id = window.turnstile.render(ensureContainer(), options);
      if (id) { window.__solverTsWidgetId = String(id); }
      window.__solverTsStage = "rendered";
      return true;
    } catch (err) {
      window.__solverTsError = String(err);
      window.__solverTsStage = "render-error";
      return false;
    }
  }

  if (renderWidget()) { return "rendered"; }

  if (!document.querySelector('script[data-solver-turnstile]')) {
    var script = document.createElement("script");
    script.src = scriptUrl;
    script.async = true;
    script.defer = true;
    script.setAttribute("data-solver-turnstile", "1");
    script.onload = function () {
      window.__solverTsScript = "loaded";
      renderWidget();
    };
    script.onerror = function () {
      window.__solverTsScript = "error";
      window.__solverTsError = "script_load_failed";
    };
    window.__solverTsScript = "loading";
    window.__solverTsStage = "script-added";
    (document.head || document.documentElement).appendChild(script);
  }

  var attempts = 0;
  var timer = setInterval(function () {
    attempts += 1;
    if (window.__solverTsToken) { clearInterval(timer); return; }
    if (renderWidget() || attempts > 300) { clearInterval(timer); }
  }, 100);
  return "loading";
"""


class SolveFailed(Exception):
    """The solver could not produce a token."""

    def __init__(self, stage: str, message: str = ""):
        super().__init__(message or stage)
        self.stage = stage
        self.message = message or stage


@dataclass
class TokenResult:
    token: str
    user_agent: str
    elapsed_ms: int


def _js_string(value) -> str:
    """JSON-encode a value for safe interpolation into a JS expression."""
    encoded = json.dumps(str(value))
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _js_optional_string(value) -> str:
    """``null`` for an absent value, otherwise a safely quoted JS string."""
    if value is None or value == "":
        return "null"
    return _js_string(value)


def build_injection_script(
    sitekey: str,
    theme: str = "dark",
    size: str = "normal",
    appearance: str = "always",
    action: str | None = None,
    c_data: str | None = None,
) -> str:
    return _INJECTION_TEMPLATE % {
        "sitekey": _js_string(sitekey),
        "theme": _js_string(theme),
        "size": _js_string(size),
        "appearance": _js_string(appearance),
        "action": _js_optional_string(action),
        "c_data": _js_optional_string(c_data),
        "script_url": _js_string(TURNSTILE_SCRIPT_URL),
        "container_id": _js_string(_CONTAINER_ID),
    }


_PASSIVE_WINDOW_SECONDS = 20.0


class TurnstileSolver:
    def __init__(self, headless: bool = False, timeout: float = 60.0, poll: float = 0.5):
        self.headless = headless
        self.timeout = timeout
        self.poll = poll
        self._driver = None

    def _start_driver(self):
        if self._driver is None:
            from seleniumbase import Driver

            kwargs = {"uc": True, "headless": self.headless}
            extra = [
                arg.strip()
                for arg in os.getenv("CHROME_ARGS", "").split(",")
                if arg.strip()
            ]
            if extra:
                # SeleniumBase takes comma separated Chromium switches.
                kwargs["chromium_arg"] = ",".join(extra)
            self._driver = Driver(**kwargs)
        return self._driver

    def close(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:  # pragma: no cover - browser is already gone
            log.warning("driver.quit() failed", exc_info=True)
        finally:
            self._driver = None

    def get_token(
        self,
        url: str,
        sitekey: str,
        theme: str = "dark",
        size: str = "normal",
        appearance: str = "always",
        action: str | None = None,
        c_data: str | None = None,
        execute: bool = False,
        timeout: float | None = None,
    ) -> TokenResult:
        limit = max(0.0, self.timeout if timeout is None else timeout)
        started = time.monotonic()
        deadline = started + limit
        # Leave room for the click fallback: an interactive challenge can never
        # solve itself, so passive polling must not consume the whole budget.
        passive_deadline = min(
            deadline, started + min(limit * 0.5, _PASSIVE_WINDOW_SECONDS)
        )
        driver = self._start_driver()
        try:
            driver.set_page_load_timeout(max(1.0, limit))
            driver.set_script_timeout(max(1.0, limit))
            driver.get(url)
            injection = driver.execute_script(
                build_injection_script(
                    sitekey,
                    theme=theme,
                    size=size,
                    appearance=appearance,
                    action=action,
                    c_data=c_data,
                )
            )
            log.info("turnstile widget injected on %s (result=%s)", url, injection)
            # ``execute`` mode runs the challenge explicitly instead of waiting
            # for a checkbox, so trigger it as soon as the widget is ready.
            executed = False
            if execute:
                self._wait_for_widget(driver, min(deadline, time.monotonic() + 15.0))
                self._kick_execute(driver)
                executed = True
            token = self._wait_for_token(driver, passive_deadline)
            clicks = 0
            while not token and clicks < 2 and time.monotonic() < deadline:
                clicks += 1
                log.info("no passive token yet; trying the click fallback (%d/2)", clicks)
                clicked = self._click_injected_widget(driver)
                if not clicked and not executed:
                    executed = True
                    self._kick_execute(driver)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait_until = (
                    deadline
                    if clicks >= 2
                    else min(deadline, time.monotonic() + remaining / 2)
                )
                token = self._wait_for_token(driver, wait_until)
                if not token and clicks == 1 and not executed:
                    executed = True
                    self._kick_execute(driver)
            try:
                token = self._validate(token)
            except SolveFailed as exc:
                diagnostics = self._diagnostics(driver)
                raise SolveFailed(
                    "no_token",
                    f"{exc.message}; diagnostics="
                    f"{json.dumps(diagnostics, ensure_ascii=False)}",
                ) from None
        except SolveFailed:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise SolveFailed("browser_error", str(exc)) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("turnstile token acquired (len=%d) in %dms", len(token), elapsed_ms)
        user_agent = driver.execute_script("return navigator.userAgent;") or ""
        return TokenResult(token=token, user_agent=user_agent, elapsed_ms=elapsed_ms)

    def _wait_for_token(self, driver, deadline: float) -> str:
        while True:
            token = driver.execute_script(READ_TOKEN_JS)
            if isinstance(token, str) and token.strip():
                return token.strip()
            if time.monotonic() >= deadline:
                return ""
            time.sleep(self.poll)

    def _wait_for_widget(self, driver, deadline: float) -> str:
        """Wait until ``turnstile.render`` returned a widget id."""
        while True:
            widget_id = driver.execute_script("return window.__solverTsWidgetId || '';")
            if isinstance(widget_id, str) and widget_id.strip():
                return widget_id.strip()
            if time.monotonic() >= deadline:
                return ""
            time.sleep(self.poll)

    def _validate(self, token) -> str:
        if not isinstance(token, str) or not token.strip():
            raise SolveFailed("no_token", "turnstile widget produced no token")
        return token.strip()

    def _diagnostics(self, driver) -> dict:
        try:
            data = driver.execute_script(DIAGNOSTICS_JS)
        except Exception as exc:  # pragma: no cover - page is already broken
            return {"diagnostics_error": str(exc)}
        return data if isinstance(data, dict) else {"raw": str(data)}

    def _click_injected_widget(self, driver) -> bool:
        """CDP-click our own widget's checkbox.

        Turnstile keeps the challenge iframe inside a *closed* shadow root, so
        it can never be found with querySelector.  The light DOM still holds the
        widget wrapper (the element that owns the hidden response input), and
        the checkbox sits ~30px from its left edge.
        """
        try:
            box = driver.execute_script(CLICK_WIDGET_JS)
            if not isinstance(box, dict):
                return False
            if "error" in box:
                log.info("cannot click the widget yet: %s", box.get("error"))
                return False
            x = float(box["x"])
            y = float(box["y"])
            hit = str(box.get("hit") or "none")
            log.info(
                "clicking the turnstile widget at (%.0f, %.0f) hit=%s rect=%s",
                x, y, hit, box.get("rect"),
            )
            self._focus_page(driver)
            # A short, curved approach looks less robotic than a teleport.
            for dx, dy in ((-45, -25), (-30, -15), (-18, -8), (-8, -4), (0, 0)):
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": x + dx,
                        "y": y + dy,
                        "button": "none",
                        "clickCount": 0,
                    },
                )
                time.sleep(0.03)
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
            time.sleep(0.05)
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
            )
            return True
        except Exception:
            log.warning("CDP click on the injected widget failed", exc_info=True)
            return False

    def _focus_page(self, driver) -> None:
        """Keep the tab focused; Cloudflare treats blurred tabs as suspicious."""
        for command, params in (
            ("Page.bringToFront", {}),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ):
            try:
                driver.execute_cdp_cmd(command, params)
            except Exception:
                log.debug("%s failed", command, exc_info=True)

    def _kick_execute(self, driver) -> str:
        """Ask Turnstile to run the challenge (needed for invisible widgets)."""
        try:
            result = driver.execute_script(EXECUTE_WIDGET_JS)
        except Exception:
            log.warning("turnstile.execute() failed", exc_info=True)
            return "error"
        log.info("turnstile.execute() -> %s", result)
        return str(result)
