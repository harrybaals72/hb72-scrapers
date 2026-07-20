"""Stash scene scraper for fc2cmadb.com.

The site is a Laravel/Inertia application protected by Cloudflare Turnstile.
Credentials are read from the local ``config.ini`` managed by ``py_common``;
the scraper uses FlareSolverr to obtain a Turnstile token, then logs in via
HTTPS before scraping. Browser cookie exports are not required.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

import fcntl

from py_common import log
from py_common.types import ScrapedScene
from py_common.util import scraper_args

ensure_deps = ["requests"]

import requests


SITE_HOST = "fc2cmadb.com"
FLARESOLVERR_URL = os.environ.get(
    "FLARESOLVERR_URL", "http://9.9.9.200:8191/v1"
)
LOGIN_URL = f"https://{SITE_HOST}/login"
REQUEST_TIMEOUT = float(os.environ.get("FC2CMADB_TIMEOUT", "30"))
FLARESOLVERR_TIMEOUT_MS = int(os.environ.get("FC2CMADB_FLARESOLVERR_TIMEOUT_MS", "90000"))
TURNSTILE_WIDGET_WAIT_SECONDS = float(
    os.environ.get("FC2CMADB_TURNSTILE_WIDGET_WAIT_SECONDS", "2")
)
TURNSTILE_TAB_COUNT = int(os.environ.get("FC2CMADB_TURNSTILE_TAB_COUNT", "8"))
RATE_LIMIT_STATE_FILE = os.environ.get(
    "FC2CMADB_RATE_STATE_FILE", str(Path(__file__).with_name("rate_state.json"))
)
ARTICLE_ID_RE = re.compile(r"(?<!\d)(\d{5,})(?!\d)")

# py_common manages a local config.ini next to the scraper. Do not log these
# values: they are long-lived account credentials.
try:
    from py_common.config import get_config

    _config = get_config(
        default="""
# FC2MADB login credentials. Keep this file private.
fc2cmadb_email =
fc2cmadb_password =
"""
    )
except Exception:
    _config = {}


def _config_value(*names: str) -> str:
    for name in names:
        try:
            value = _config[name]
        except (KeyError, TypeError, AttributeError):
            value = ""
        if value:
            if isinstance(value, bool):
                return str(value).lower()
            return str(value).strip()
    return ""


def _credentials() -> tuple[str, str]:
    """Return configured credentials without ever logging their values."""
    return (
        _config_value("fc2cmadb_email"),
        _config_value("fc2cmadb_password"),
    )


def _cookie_payload(cookies: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"name": name, "value": value, "domain": SITE_HOST, "path": "/"}
        for name, value in cookies.items()
        if value
    ]


def _set_cookie(session: requests.Session, name: str, value: str, **kwargs: Any) -> None:
    try:
        session.cookies.set(name, value, **kwargs)
    except (TypeError, ValueError):
        # Be tolerant of malformed domain/path metadata returned by a
        # FlareSolverr version while still retaining the cookie value.
        session.cookies.set(name, value)


def _new_session(solution: dict[str, Any] | None, cookies: dict[str, str] | None = None) -> requests.Session:
    """Build a direct HTTPS session from a FlareSolverr solution."""
    session = requests.Session()
    # Direct connections avoid accidentally routing fc2cmadb through a proxy
    # that may expose or alter the authenticated request.
    session.proxies = {}
    session.trust_env = False

    for name, value in (cookies or {}).items():
        _set_cookie(session, name, value, domain=f".{SITE_HOST}", path="/")

    if solution:
        user_agent = solution.get("userAgent")
        if user_agent:
            session.headers["User-Agent"] = str(user_agent)
        for cookie in solution.get("cookies", []):
            if not isinstance(cookie, dict) or not cookie.get("name"):
                continue
            _set_cookie(
                session,
                str(cookie["name"]),
                str(cookie.get("value", "")),
                domain=cookie.get("domain") or SITE_HOST,
                path=cookie.get("path") or "/",
            )

    return session


def _session_cookies(session: requests.Session) -> dict[str, str]:
    """Return the current session cookies for FlareSolverr recovery."""
    return {str(cookie.name): str(cookie.value) for cookie in session.cookies}


def _flaresolverr_command(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Issue one FlareSolverr command without exposing sensitive response data."""
    try:
        response = requests.post(
            FLARESOLVERR_URL,
            json=payload,
            timeout=max(REQUEST_TIMEOUT, FLARESOLVERR_TIMEOUT_MS / 1000),
        )
    except requests.RequestException as exc:
        log.warning(f"Unable to contact FlareSolverr at {FLARESOLVERR_URL}: {exc}")
        return None
    if response.status_code != 200:
        log.warning(f"FlareSolverr returned HTTP {response.status_code}")
        return None
    try:
        body = response.json()
    except ValueError:
        log.warning("FlareSolverr returned invalid JSON")
        return None
    if not isinstance(body, dict) or body.get("status") not in (None, "ok"):
        log.warning(f"FlareSolverr command failed: {body.get('message', 'unknown error') if isinstance(body, dict) else 'invalid response'}")
        return None
    return body


def _get_flaresolverr_solution(url: str, cookies: dict[str, str]) -> dict[str, Any] | None:
    """Return a FlareSolverr GET solution, or None when it is unavailable."""
    body = _flaresolverr_command(
        {
            "cmd": "request.get",
            "url": url,
            "cookies": _cookie_payload(cookies),
            "session_ttl_minutes": 5,
            "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
        }
    )
    if not body or not isinstance(body.get("solution"), dict):
        log.warning(f"FlareSolverr did not return a usable solution for {url}")
        return None
    return body["solution"]


def _login_page(response_text: str, response_url: str = "") -> bool:
    return "/login" in response_url.lower() or "https://fc2cmadb.com/login" in response_text.lower()


def _inertia_version(html: str) -> str:
    match = re.search(r'<script[^>]*data-page=["\']app["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        return ""
    try:
        return str(json.loads(match.group(1)).get("version") or "")
    except (TypeError, json.JSONDecodeError):
        return ""


def _inertia_page_data(html: str) -> dict[str, Any] | None:
    """Extract Inertia page props from the initial HTML response.

    Inertia embeds the full page data in a <script data-page="app"> tag.
    Returns the ``props`` dict, or None if it cannot be parsed.
    """
    match = re.search(
        r'<script[^>]*data-page=["\']app["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    props = parsed.get("props") if isinstance(parsed, dict) else None
    return props if isinstance(props, dict) else None



# ---------------------------------------------------------------------------
# Rate-limit state persistence
#
# The site sends X-RateLimit-Limit: 3 and X-RateLimit-Remaining: N (Laravel
# throttle middleware).  Since each script invocation is an independent
# process, we use a small JSON file to track cooldown state across calls.
# ---------------------------------------------------------------------------


_RATE_STATE_VERSION = 1
_DEFAULT_WINDOW_SECONDS = 5
_ZERO_REMAINING_COOLDOWN_SECONDS = 30


def _load_rate_state() -> dict[str, Any]:
    """Load rate-limit state from ``RATE_LIMIT_STATE_FILE``."""
    path = Path(RATE_LIMIT_STATE_FILE)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == _RATE_STATE_VERSION:
                remaining = raw.get("remaining", "?")
                cooldown = raw.get("cooldown_until", 0.0)
                log.info(
                    f"[fc2madb.py] Rate-limit state loaded: remaining={remaining}, "
                    f"cooldown={'{:.1f}s'.format(cooldown - time.time()) if cooldown > time.time() else 'none'}"
                )
                return raw
        except (OSError, json.JSONDecodeError) as exc:
            log.debug(f"Unable to read rate state {path}: {exc}")
    log.info("[fc2madb.py] Rate-limit state: fresh start (no prior state file)")
    return {
        "version": _RATE_STATE_VERSION,
        "cooldown_until": 0.0,
        "window_start": 0.0,
        "limit": 3,
        "remaining": 3,
    }


def _save_rate_state(state: dict[str, Any]) -> None:
    """Atomically write rate-limit state to ``RATE_LIMIT_STATE_FILE``."""
    path = Path(RATE_LIMIT_STATE_FILE)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        log.warning(f"Unable to write rate state {path}: {exc}")


def _wait_for_cooldown(state: dict[str, Any]) -> None:
    """Sleep until the cooldown period expires, if applicable."""
    remaining = state["cooldown_until"] - time.time()
    if remaining > 0:
        log.info(
            f"[fc2madb.py] Rate-limit cooldown: sleeping {remaining:.1f}s "
            f"(remaining={state.get('remaining', '?')}, "
            f"limit={state.get('limit', '?')})"
        )
        time.sleep(remaining)


def _update_rate_state_from_headers(
    state: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Update rate-limit state from response headers.

    Looks for ``X-RateLimit-Remaining`` and optionally
    ``X-RateLimit-Reset`` / ``Retry-After``.  If the remaining count has
    dropped to 1 or 0, we set a cooldown so the next invocation waits.
    """
    now = time.time()

    # Extract headers (case-insensitive lookup)
    limit_str = ""
    remaining_str = ""
    reset_str = ""
    retry_str = ""
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "x-ratelimit-limit":
            limit_str = v
        elif k_lower == "x-ratelimit-remaining":
            remaining_str = v
        elif k_lower == "x-ratelimit-reset":
            reset_str = v
        elif k_lower == "retry-after":
            retry_str = v

    if limit_str:
        try:
            state["limit"] = int(limit_str)
        except (TypeError, ValueError):
            log.warning(
                f"[fc2madb.py] Rate-limit header unparseable: "
                f"X-RateLimit-Limit={limit_str!r}"
            )

    if not remaining_str:
        log.info(
            "[fc2madb.py] Rate-limit headers not present in response — "
            "site may not be throttling this request"
        )
        return state

    try:
        remaining = int(remaining_str)
    except (TypeError, ValueError):
        log.warning(
            f"[fc2madb.py] Rate-limit header unparseable: "
            f"X-RateLimit-Remaining={remaining_str!r}"
        )
        return state

    state["remaining"] = remaining

    # Determine cooldown duration
    cooldown = 0.0
    cooldown_source = ""

    if reset_str:
        try:
            cooldown = float(reset_str) - now
            cooldown_source = "X-RateLimit-Reset"
        except (TypeError, ValueError):
            cooldown = 0.0
    elif retry_str:
        try:
            cooldown = float(retry_str)
            cooldown_source = "Retry-After"
        except (TypeError, ValueError):
            cooldown = 0.0

    if remaining == 0:
        # Zero remaining is an exhausted quota. Use the explicit safety
        # cooldown even when the server supplies no reset/retry header.
        cooldown = _ZERO_REMAINING_COOLDOWN_SECONDS
        cooldown_source = "zero remaining"
    elif cooldown <= 0 and remaining == 1:
        # No explicit reset time — use a heuristic: if the window started
        # recently, estimate the remaining window duration.
        elapsed = now - state.get("window_start", 0.0)
        if elapsed < _DEFAULT_WINDOW_SECONDS and elapsed > 0:
            cooldown = _DEFAULT_WINDOW_SECONDS - elapsed
            cooldown_source = "heuristic (window {:.0f}s ago)".format(elapsed)
        else:
            cooldown = _DEFAULT_WINDOW_SECONDS
            cooldown_source = "heuristic (default window)"
        # Start a new window tracking point if we don't have one
        if state.get("window_start", 0.0) <= 0 or elapsed >= _DEFAULT_WINDOW_SECONDS:
            state["window_start"] = now

    if cooldown > 0:
        state["cooldown_until"] = now + cooldown
        log.info(
            f"[fc2madb.py] Rate-limit: remaining={remaining}/{state.get('limit', '?')}, "
            f"cooldown={cooldown:.0f}s ({cooldown_source})"
        )
    else:
        state["cooldown_until"] = 0.0
        if remaining <= 1:
            log.info(
                f"[fc2madb.py] Rate-limit: remaining={remaining}/{state.get('limit', '?')}, "
                f"no cooldown needed"
            )

    return state


def _force_rate_limit_cooldown(state: dict[str, Any]) -> None:
    """Force the zero-remaining cooldown for a 429 without rate headers."""
    now = time.time()
    cooldown = _ZERO_REMAINING_COOLDOWN_SECONDS
    state["remaining"] = 0
    state["cooldown_until"] = now + cooldown
    state["window_start"] = now


def _record_response(state: dict[str, Any], response: Any) -> None:
    """Persist rate state immediately after every origin response."""
    _update_rate_state_from_headers(state, dict(getattr(response, "headers", {})))
    if getattr(response, "status_code", 0) == 429:
        _force_rate_limit_cooldown(state)
    _save_rate_state(state)


def _record_solution_response(
    state: dict[str, Any], solution: dict[str, Any]
) -> int | None:
    """Persist headers/status carried by a FlareSolverr origin response.

    FlareSolverr versions do not consistently include the origin status in a
    solution.  ``None`` preserves that uncertainty so the Inertia props can
    provide the authoritative status when available.
    """
    raw_status = solution.get("status")
    if raw_status is None:
        raw_status = solution.get("statusCode")
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    headers = solution.get("headers") if isinstance(solution.get("headers"), dict) else {}
    _update_rate_state_from_headers(state, headers)
    if status == 429:
        _force_rate_limit_cooldown(state)
    _save_rate_state(state)
    return status


@contextmanager
def _rate_state_lock() -> Iterator[None]:
    """Serialize rate-state coordination and all FC2MADB requests."""
    lock_path = Path(f"{RATE_LIMIT_STATE_FILE}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _solution_cookie_value(solution: dict[str, Any], name: str) -> str:
    """Return a named FlareSolverr cookie value without logging it."""
    for cookie in solution.get("cookies", []):
        if isinstance(cookie, dict) and cookie.get("name") == name:
            return str(cookie.get("value") or "")
    return ""


def _login_succeeded(response: requests.Response) -> bool:
    """Recognize Laravel/Inertia's successful external redirect response."""
    if response.status_code in (302, 303):
        location = str(response.headers.get("Location") or "")
        return bool(location) and not urlparse(location).path.startswith("/login")
    location = str(response.headers.get("X-Inertia-Location") or "")
    return response.status_code == 409 and location.startswith(f"https://{SITE_HOST}")


def _login_with_credentials(
    rate_state: dict[str, Any], email: str, password: str
) -> requests.Session | None:
    """Solve Turnstile through FlareSolverr and return an authenticated session.

    The FlareSolverr browser is used only for the dynamically rendered
    Turnstile widget. Credentials are posted directly to fc2cmadb over HTTPS.
    Every solution or direct response that represents an origin request is
    recorded before another request is attempted.
    """
    flaresolverr_session = f"fc2madb-{secrets.token_hex(12)}"
    created = False
    try:
        created_response = _flaresolverr_command(
            {"cmd": "sessions.create", "session": flaresolverr_session}
        )
        if not created_response:
            return None
        created = True

        warm_response = _flaresolverr_command(
            {
                "cmd": "request.get",
                "url": LOGIN_URL,
                "session": flaresolverr_session,
                "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
                "waitInSeconds": 1,
            }
        )
        warm_solution = warm_response.get("solution") if warm_response else None
        if not isinstance(warm_solution, dict):
            log.error("[fc2madb.py] FAILURE TYPE=login  FlareSolverr did not render the login page")
            return None
        if _record_solution_response(rate_state, warm_solution) == 429:
            log.error("[fc2madb.py] FAILURE TYPE=http_429  login page rate limit exceeded")
            return None

        # The site inserts the Turnstile response input after React/Inertia
        # initializes. Keep the same browser page and use only a hash change
        # so FlareSolverr's immediate selector check can see that input.
        time.sleep(TURNSTILE_WIDGET_WAIT_SECONDS)
        solved_response = _flaresolverr_command(
            {
                "cmd": "request.get",
                "url": f"{LOGIN_URL}#flaresolverr-turnstile",
                "session": flaresolverr_session,
                "tabs_till_verify": TURNSTILE_TAB_COUNT,
                "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
                "waitInSeconds": 1,
            }
        )
        solution = solved_response.get("solution") if solved_response else None
        if not isinstance(solution, dict):
            log.error("[fc2madb.py] FAILURE TYPE=login  FlareSolverr did not solve Turnstile")
            return None
        if _record_solution_response(rate_state, solution) == 429:
            log.error("[fc2madb.py] FAILURE TYPE=http_429  Turnstile request rate limit exceeded")
            return None

        token = str(solution.get("turnstile_token") or "")
        xsrf_token = _solution_cookie_value(solution, "XSRF-TOKEN")
        if not token or not xsrf_token:
            log.error("[fc2madb.py] FAILURE TYPE=login  Turnstile token or XSRF cookie missing")
            return None

        # Honor any server-provided or heuristic cooldown before the direct
        # credential POST. This prevents login plus article retrieval from
        # exceeding the site's three-request throttle window.
        _wait_for_cooldown(rate_state)
        session = _new_session(solution)
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": LOGIN_URL,
                "X-Inertia": "true",
                "X-Requested-With": "XMLHttpRequest",
                "X-XSRF-TOKEN": unquote(xsrf_token),
            }
        )
        try:
            response = session.post(
                LOGIN_URL,
                json={
                    "email": email,
                    "password": password,
                    "remember": True,
                    "token": token,
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log.error(f"[fc2madb.py] FAILURE TYPE=login  credential request failed: {exc}")
            return None
        _record_response(rate_state, response)
        if response.status_code == 429:
            log.error("[fc2madb.py] FAILURE TYPE=http_429  login request rate limit exceeded")
            return None
        if not _login_succeeded(response):
            log.error(
                f"[fc2madb.py] FAILURE TYPE=login  credential request returned HTTP {response.status_code}"
            )
            return None
        return session
    finally:
        if created:
            _flaresolverr_command(
                {"cmd": "sessions.destroy", "session": flaresolverr_session}
            )


def _authenticated(props: Any) -> bool:
    auth = props.get("auth") if isinstance(props, dict) else None
    return isinstance(auth, dict) and isinstance(auth.get("user"), dict)


def _error_message(props: Any, status: int) -> str:
    if isinstance(props, dict) and props.get("message"):
        return f"HTTP {status} - {props['message']}"
    return f"HTTP {status}"


def _article_id_from_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r"/articles/(\d{5,})(?:[/?#]|$)", value)
    if match:
        return match.group(1)
    match = ARTICLE_ID_RE.search(value)
    return match.group(1) if match else ""


def _article_id_from_url(url: str) -> str:
    """Extract the numeric article ID from a full fc2cmadb article URL."""
    match = re.search(r"/articles/(\d{5,})(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def _url_from_fragment(args: dict[str, Any]) -> str:
    for value in args.get("urls", []) if isinstance(args.get("urls"), list) else []:
        article_id = _article_id_from_value(value)
        if article_id:
            return f"https://{SITE_HOST}/articles/{article_id}"

    for key in ("url", "code"):
        article_id = _article_id_from_value(args.get(key))
        if article_id:
            return f"https://{SITE_HOST}/articles/{article_id}"

    files = args.get("files", [])
    if isinstance(files, list):
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            article_id = _article_id_from_value(str(file_info.get("path", "")))
            if article_id:
                return f"https://{SITE_HOST}/articles/{article_id}"

    # Stash may use the filename as title when no title was entered.
    article_id = _article_id_from_value(args.get("title"))
    if article_id:
        return f"https://{SITE_HOST}/articles/{article_id}"
    return ""


def _duration_seconds(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    try:
        numbers = [int(part) for part in parts]
        if len(numbers) == 3:
            hours, minutes, seconds = numbers
            return hours * 3600 + minutes * 60 + seconds
        if len(numbers) == 2:
            minutes, seconds = numbers
            return minutes * 60 + seconds
    except ValueError:
        pass
    return None


def _scene_from_url_locked(url: str, email: str, password: str) -> ScrapedScene:
    rate_state = _load_rate_state()
    _wait_for_cooldown(rate_state)
    session = _login_with_credentials(rate_state, email, password)
    if session is None:
        return {}
    # A successful login commonly leaves one request in the throttle window.
    # Wait before the article GET so login and scraping cannot trigger a 429.
    _wait_for_cooldown(rate_state)
    initial_html = ""
    initial_status: int | None = 200
    initial_props: dict[str, Any] | None = None
    solution: dict[str, Any] | None = None

    try:
        initial = session.get(url, timeout=REQUEST_TIMEOUT)
        _record_response(rate_state, initial)
        initial_status = initial.status_code
        if initial_status == 429:
            log.error(f"[fc2madb.py] FAILURE TYPE=http_429  URL={url}  rate limit exceeded")
            return {}
        if _login_page(initial.text, initial.url):
            log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  login prompt in direct fetch")
            return {}
        if initial_status == 403 and "1005" in initial.text:
            solution = _get_flaresolverr_solution(url, _session_cookies(session))
            if not solution:
                log.error(
                    f"[fc2madb.py] FAILURE TYPE=cloudflare  URL={url}  "
                    "ASN block and no FlareSolverr fallback"
                )
                return {}
            initial_status = _record_solution_response(rate_state, solution)
            if initial_status == 429:
                log.error(f"[fc2madb.py] FAILURE TYPE=http_429  URL={url}  rate limit exceeded")
                return {}
            initial_html = str(solution.get("response", ""))
            session = _new_session(solution, _session_cookies(session))
        else:
            initial_html = initial.text
    except requests.RequestException as exc:
        # A timeout or connection error may occur after the request reached
        # FC2MADB. Do not issue another origin request through FlareSolverr.
        log.error(f"[fc2madb.py] FAILURE TYPE=unreachable  URL={url}  {exc}")
        return {}

    if _login_page(initial_html):
        log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  login prompt in initial response")
        return {}

    initial_props = _inertia_page_data(initial_html)
    if initial_props is None and initial_status in (200, None):
        # Some deployments return an empty shell to the direct request. Ask
        # Inertia for the full page, then classify that response normally.
        version = _inertia_version(initial_html)
        if not version:
            log.error(
                f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  "
                "no Inertia version in HTML"
            )
            return {}
        headers = {
            "X-Inertia": "true",
            "X-Requested-With": "XMLHttpRequest",
            "X-Inertia-Version": version,
            "Referer": url,
            "Accept": "text/html, application/xhtml+xml",
            "Cache-Control": "no-cache",
        }
        try:
            info_response = session.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
            _record_response(rate_state, info_response)
            if _login_page(info_response.text, info_response.url):
                log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  Inertia GET redirected to login")
                return {}
            if info_response.status_code == 429:
                log.error(f"[fc2madb.py] FAILURE TYPE=http_429  URL={url}  rate limit exceeded")
                return {}
            try:
                payload = info_response.json()
            except ValueError as exc:
                log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  {exc}")
                return {}
            initial_status = info_response.status_code
            initial_props = payload.get("props") if isinstance(payload, dict) else None
            if not isinstance(initial_props, dict):
                log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  invalid Inertia props")
                return {}
        except requests.RequestException as exc:
            log.error(f"[fc2madb.py] FAILURE TYPE=unreachable  URL={url}  {exc}")
            return {}

    # FlareSolverr may omit the origin status. In that case, use the
    # authenticated Inertia props (especially props.status for error pages).
    if initial_status is None:
        raw_status = initial_props.get("status") if isinstance(initial_props, dict) else None
        try:
            initial_status = int(raw_status) if raw_status is not None else 200
        except (TypeError, ValueError):
            initial_status = 200

    # Auth is deliberately checked before interpreting a 404. An error page
    # without auth data cannot prove that the supplied session is logged in.
    if not _authenticated(initial_props):
        log.error(
            f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  "
            "cookies are expired or invalid — user is not logged in"
        )
        return {}

    if initial_status == 404:
        log.error(
            f"[fc2madb.py] FAILURE TYPE=http_404  URL={url}  "
            f"{_error_message(initial_props, initial_status)}"
        )
        return {"tags": [{"name": "FC2MADB 404"}]}
    if initial_status != 200:
        log.error(
            f"[fc2madb.py] FAILURE TYPE=http_{initial_status}  URL={url}  "
            f"{_error_message(initial_props, initial_status)}"
        )
        return {}

    article = initial_props.get("article")
    if not isinstance(article, dict):
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  article object missing")
        return {}

    version = _inertia_version(initial_html)
    if not version:
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  no Inertia version in HTML")
        return {}

    # actresses is a deferred Inertia prop. A complete scrape requires this
    # second successful request, even when the returned list is empty.
    inertia_headers = {
        "X-Inertia": "true",
        "X-Requested-With": "XMLHttpRequest",
        "X-Inertia-Partial-Component": "Articles/Show",
        "X-Inertia-Partial-Data": "article,actresses",
        "X-Inertia-Version": version,
        "Referer": url,
        "Accept": "application/json, text/plain, */*",
    }
    try:
        info_response = session.get(url, timeout=REQUEST_TIMEOUT, headers=inertia_headers)
        _record_response(rate_state, info_response)
        if _login_page(info_response.text, info_response.url):
            log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  deferred GET redirected to login")
            return {}
        if info_response.status_code == 429:
            log.error(f"[fc2madb.py] FAILURE TYPE=http_429  URL={url}  rate limit exceeded")
            return {}
        if info_response.status_code == 409:
            new_version = _inertia_version(info_response.text)
            if not new_version or new_version == version:
                log.error(f"[fc2madb.py] FAILURE TYPE=http_409  URL={url}  version mismatch")
                return {}
            inertia_headers["X-Inertia-Version"] = new_version
            retry = session.get(url, timeout=REQUEST_TIMEOUT, headers=inertia_headers)
            _record_response(rate_state, retry)
            if _login_page(retry.text, retry.url) or retry.status_code != 200:
                log.error(f"[fc2madb.py] FAILURE TYPE=http_{retry.status_code}  URL={url}  deferred retry failed")
                return {}
            info_response = retry
        if info_response.status_code != 200:
            log.error(
                f"[fc2madb.py] FAILURE TYPE=http_{info_response.status_code}  URL={url}  "
                "deferred actresses request failed"
            )
            return {}
        payload = info_response.json()
    except (requests.RequestException, ValueError) as exc:
        log.error(f"[fc2madb.py] FAILURE TYPE=performers  URL={url}  {exc}")
        return {}

    if not isinstance(payload, dict) or payload.get("component") != "Articles/Show":
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  unexpected Inertia component")
        return {}
    deferred_props = payload.get("props")
    actresses = deferred_props.get("actresses") if isinstance(deferred_props, dict) else None
    if not isinstance(actresses, list):
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  actresses list missing")
        return {}

    scene: ScrapedScene = {}
    title = str(article.get("title") or "").strip()
    description = str(article.get("description") or article.get("details") or "").strip()
    if title and description:
        scene["details"] = f"{title}\n{description}"
    elif title or description:
        scene["details"] = title or description

    video_id = article.get("video_id")
    if video_id:
        scene["code"] = f"FC2-PPV-{video_id}"
    if article.get("release_date"):
        scene["date"] = str(article["release_date"])[:10]
    if article.get("image_url"):
        scene["image"] = str(article["image_url"])
    if (duration := _duration_seconds(article.get("duration"))) is not None:
        scene["duration"] = duration

    writer = article.get("writer")
    if isinstance(writer, dict) and writer.get("name"):
        scene["studio"] = {"name": str(writer["name"]).strip()}

    tags = article.get("tags")
    if isinstance(tags, list):
        scene["tags"] = [
            {"name": str(tag["name"]).strip()}
            for tag in tags
            if isinstance(tag, dict) and tag.get("name")
        ]
    scene["performers"] = [
        {"name": str(performer["name"]).strip()}
        for performer in actresses
        if isinstance(performer, dict) and performer.get("name")
    ]
    scene["urls"] = [url]
    return scene


def scene_from_url(url: str) -> ScrapedScene:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {
        SITE_HOST,
        f"www.{SITE_HOST}",
    }:
        log.error(f"[fc2madb.py] FAILURE TYPE=unsupported_url  URL={url}")
        return {}

    email, password = _credentials()
    if not email or not password:
        log.error(
            f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  missing credentials. "
            "Set fc2cmadb_email and fc2cmadb_password in config.ini."
        )
        return {}
    with _rate_state_lock():
        return _scene_from_url_locked(url, email, password)


if __name__ == "__main__":
    operation, args = scraper_args()
    result: ScrapedScene

    if operation == "scene-by-url" and args.get("url"):
        result = scene_from_url(str(args["url"]))
    elif operation in {"scene-by-fragment", "scene-by-query-fragment"}:
        url = _url_from_fragment(args)
        if not url:
            log.warning(
                f"[fc2madb.py] No FC2 article ID found in fragment; nothing to scrape. "
                f"Fragment keys: {list(args.keys())}"
            )
            result = {}
        else:
            result = scene_from_url(url)
    else:
        log.error(f"[fc2madb.py] Unsupported operation: {operation}; arguments: {json.dumps(args)}")
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False))
