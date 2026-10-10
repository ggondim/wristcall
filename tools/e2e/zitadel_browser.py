#!/usr/bin/env python3
"""Drives the Zitadel login page as a human test user, for the end-to-end account tests.

Usage:
    zitadel_browser.py login <authorize-url | ->   # prints the app's callback URL (wristcall://auth/callback?...)
    zitadel_browser.py device <user-code | ->      # prints "approved"

`-` reads the argument from standard input (the Swift tests pass it that way, so it stays out of `ps`).

`login` opens the authorization URL the app built (OIDC Authorization Code with PKCE), signs in, skips the
"set up 2FA" / passwordless prompts, accepts a consent page if one shows, and catches the redirect to the app's
custom scheme instead of following it. `device` opens `<issuer>/device?user_code=...` (RFC 8628), signs in and
presses "Allow".

Environment:
    E2E_ZITADEL_USER            login name of the test user (required)
    E2E_ZITADEL_PASSWORD_FILE   file holding the password (preferred), or
    E2E_ZITADEL_PASSWORD        the password itself
    E2E_ZITADEL_ISSUER          default https://auth.trigram.com.br
    E2E_CALLBACK_SCHEME         default wristcall
    E2E_BROWSER_TIMEOUT         seconds for the whole run, default 90

Standard output carries only the result. Progress goes to standard error as page paths, never query strings,
so no code, state or token is printed; the password is never printed.

Exit codes: 0 done, 1 unexpected error (only its type is printed), 2 usage or configuration, 3 the provider's
login page is unavailable (e.g. a 404 on the hosted login), 4 the login did not go through (error shown, or a page
this helper does not know), 5 timeout.

Tested on Zitadel's login v1 (`/ui/login/...`, the device flow) and login v2 (`/ui/v2/login/...`, what the
authorization code flow lands on: login name, password, then a client-side redirect to the app). Login v2 is a
Next.js app, so waits are timed (a submitted field still shown after ANSWER_WITHIN seconds was refused), not
counted in page loads. Its 2FA-setup and consent pages were not seen with the test user.

Secrets: Playwright writes filled values into its error call logs, so no exception text ever reaches the output:
a failed fill becomes a fixed message, and any other exception prints only its type.

Needs Playwright for Python (`pip install playwright`, then `playwright install --only-shell chromium`).
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse

SKIP_LABELS = ("Skip", "Überspringen", "Not now", "Later", "Pular", "Agora não")
# Device approval and consent buttons. Not "Continue"/"Next": on login v2 those are the login steps themselves.
ALLOW_LABELS = ("Allow", "Erlauben", "Permitir", "Accept", "Akzeptieren", "Aceitar")
# Where a login error shows: v1's `.lgn-error`; v2's `[data-testid=error]` inside the form. Never a bare
# `[role=alert]`: Next.js's route announcer (`next-route-announcer`, aria-live) has that role and says the page title.
ERROR_SELECTOR = ".lgn-error, form [data-testid=error], form [role=alert], form .error, form [data-error]"


# A submitted field still on screen after this many seconds was refused; a page unchanged this long is stuck.
ANSWER_WITHIN = 10.0
STUCK_AFTER = 30.0


class Stop(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def note(message: str) -> None:
    print(f"[zitadel-browser] {message}", file=sys.stderr, flush=True)


def path_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else f"{parts.scheme}:{parts.path}"


def password() -> str:
    path = os.environ.get("E2E_ZITADEL_PASSWORD_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    value = os.environ.get("E2E_ZITADEL_PASSWORD", "")
    if not value:
        raise Stop(2, "set E2E_ZITADEL_PASSWORD_FILE or E2E_ZITADEL_PASSWORD")
    return value


def visible(page, selector: str):
    for element in page.locator(selector).all():
        try:
            if element.is_visible():
                return element
        except Exception:  # noqa: BLE001 - the page moved on; the next round looks again
            return None
    return None


def button(page, labels: tuple[str, ...]):
    for label in labels:
        element = visible(page, f"button:text-is('{label}'), input[type=submit][value='{label}']")
        if element is not None:
            return element
    return None


def submit(page, field) -> None:
    """Presses the form's submit button (Zitadel's v1 page names it `submit-button`), else Enter."""
    target = visible(page, "#submit-button") or visible(page, "button[type=submit]:not([name=skip])")
    if target is not None:
        target.click()
    else:
        field.press("Enter")


def busy(page) -> bool:
    """True while a form is submitting: login v2 disables its submit button until the server action answers.
    A disabled button over an empty field only means "type something" (v2 does that too), so it is not busy."""
    target = visible(page, "button[type=submit]:not([name=skip])")
    return target is not None and target.evaluate(
        "b => b.disabled && !!b.form && [...b.form.querySelectorAll('input')]"
        ".filter(i => i.type !== 'hidden' && i.offsetParent !== null).every(i => i.value)"
    )


def fill_secret(field, secret: str) -> None:
    """Fills the password; a failure never carries Playwright's call log (it holds the filled value)."""
    try:
        field.fill(secret)
    except Exception:  # noqa: BLE001 - the message would contain the password
        raise Stop(4, "password field not fillable") from None


def run(mode: str, argument: str) -> str:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise Stop(2, "Playwright for Python is not installed") from None

    user = os.environ.get("E2E_ZITADEL_USER", "")
    if not user:
        raise Stop(2, "set E2E_ZITADEL_USER")
    secret = password()
    issuer = os.environ.get("E2E_ZITADEL_ISSUER", "https://auth.trigram.com.br").rstrip("/")
    scheme = os.environ.get("E2E_CALLBACK_SCHEME", "wristcall") + ":"
    deadline = time.monotonic() + float(os.environ.get("E2E_BROWSER_TIMEOUT", "90"))

    if mode == "login":
        start = argument
        if not start.startswith(issuer + "/"):
            raise Stop(2, "the authorization URL is not on the issuer")
    else:
        code = "".join(ch for ch in argument if ch.isalnum())
        if not code:
            raise Stop(2, "empty user code")
        start = f"{issuer}/device?{urllib.parse.urlencode({'user_code': argument.strip()})}"

    caught: list[str] = []

    def catch(url: str | None) -> None:
        if url and url.lower().startswith(scheme) and not caught:
            caught.append(url)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.set_default_timeout(15_000)
            # The provider sends the browser to the app with a redirect (or a script): catch it, do not follow.
            page.on("response", lambda r: catch(r.headers.get("location")))
            page.on("request", lambda r: catch(r.url))
            page.on("framenavigated", lambda f: catch(f.url))

            note(f"{mode}: opening {path_of(start)}")
            try:
                page.goto(start, wait_until="domcontentloaded")
            except PlaywrightError:
                if not caught:
                    raise
            # Login v2 is a Next.js app: a submit runs a server action, keeps the old page (and URL) on screen while it
            # runs and may never load a new document, so progress is judged by time, not by loop rounds.
            sent_user = sent_password = 0.0  # when each field was last submitted
            last = ""
            since = time.monotonic()
            while True:
                if caught:
                    return caught[0]
                if time.monotonic() > deadline:
                    raise Stop(5, f"timed out on {path_of(page.url)}")
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PlaywrightError:
                    pass
                if caught:
                    return caught[0]
                here = path_of(page.url)
                now = time.monotonic()
                if here != last:
                    note(f"page {here}")
                    last, since = here, now
                elif now - since > STUCK_AFTER:
                    raise Stop(4, f"stuck on {here}")

                body = page.locator("body").inner_text(timeout=5_000) if page.locator("body").count() else ""
                if '"code":5' in body.replace(" ", "") and "Not Found" in body:
                    raise Stop(3, f"the login page {here} answers 404 (the provider's hosted login is down)")
                error = visible(page, ERROR_SELECTOR)
                if error is not None and error.inner_text().strip():
                    raise Stop(4, f"login error on {here}: {error.inner_text().strip()[:200]}")

                if mode == "device" and (here.endswith("/allowed") or "Device authorized" in body or "Gerät autorisiert" in body):
                    return "approved"
                if mode == "device" and (here.endswith("/denied")):
                    raise Stop(4, "the device request was denied")

                if busy(page):
                    page.wait_for_timeout(500)  # not time.sleep: Playwright delivers events (the callback) only here
                    continue

                field = visible(page, "input[type=password]")
                if field is not None:
                    if now - sent_password < ANSWER_WITHIN:
                        page.wait_for_timeout(500)
                        continue
                    if sent_password:
                        raise Stop(4, f"password not accepted on {here}")
                    fill_secret(field, secret)
                    sent_password = time.monotonic()
                    submit(page, field)
                    continue
                field = visible(page, "#loginName, input[name=loginName], input[autocomplete=username]")
                if field is not None:
                    if now - sent_user < ANSWER_WITHIN:
                        page.wait_for_timeout(500)
                        continue
                    if sent_user:
                        raise Stop(4, f"login name not accepted on {here}")
                    field.fill(user)
                    sent_user = time.monotonic()
                    submit(page, field)
                    continue
                field = visible(page, "input[name=code], #code")
                if mode == "device" and field is not None and not field.input_value():
                    field.fill(argument.strip())
                    submit(page, field)
                    continue
                skip = visible(page, "button[name=skip]") or button(page, SKIP_LABELS)
                if skip is not None:
                    note("skipping the 2FA / passwordless setup prompt")
                    skip.click()
                    continue
                allow = visible(page, "button[formaction='./allowed']") or button(page, ALLOW_LABELS)
                if allow is not None:
                    note("allow" if mode == "device" else "consent")
                    allow.click()
                    continue
                page.wait_for_timeout(1_000)
        finally:
            browser.close()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("login", "device"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    argument = sys.stdin.readline().strip() if argv[2] == "-" else argv[2]
    try:
        print(run(argv[1], argument), flush=True)
        return 0
    except Stop as e:
        note(str(e))
        return e.code
    except Exception as e:  # noqa: BLE001 - only the type: Playwright's messages can hold filled values
        note(f"failed with {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
