"""Stash scene scraper for fc2cmadb.com.

The site is a Laravel/Inertia application protected by Cloudflare.  A browser
cookie export can be placed next to this script as ``cookies.json``.  The
FlareSolverr URL and cookie file can also be overridden with environment
variables, which is useful when Stash and FlareSolverr run in containers.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from py_common import log
from py_common.types import ScrapedScene
from py_common.util import scraper_args

ensure_deps = ["requests"]

import requests


SITE_HOST = "fc2cmadb.com"
FLARESOLVERR_URL = os.environ.get(
    "FLARESOLVERR_URL", "http://9.9.9.200:8191/v1"
)
COOKIE_FILE = os.environ.get(
    "FC2CMADB_COOKIE_FILE", str(Path(__file__).with_name("cookies.json"))
)
REQUEST_TIMEOUT = float(os.environ.get("FC2CMADB_TIMEOUT", "30"))
RATE_LIMIT_STATE_FILE = os.environ.get(
    "FC2CMADB_RATE_STATE_FILE", str(Path(__file__).with_name("rate_state.json"))
)
ARTICLE_ID_RE = re.compile(r"(?<!\d)(\d{5,})(?!\d)")

# These are only fallback names for py_common's optional config.ini.  The
# browser export is preferred so that the supplied cookies.json works without
# any manual copy/paste into another configuration file.
try:
    from py_common.config import get_config

    _config = get_config(
        default="""
# Optional fallback values. Browser cookies.json or environment variables are preferred.
fc2cmadb_session =
xsrf_token =
age_verified = true
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


def _load_cookies() -> dict[str, str]:
    """Load browser-exported cookies and apply optional config/env overrides."""
    values: dict[str, str] = {}
    cookie_path = Path(COOKIE_FILE)

    if cookie_path.is_file():
        try:
            raw = json.loads(cookie_path.read_text(encoding="utf-8"))
            records = raw.get("cookies", []) if isinstance(raw, dict) else raw
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict) and record.get("name") and record.get(
                        "value"
                    ) is not None:
                        values[str(record["name"])] = str(record["value"])
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"Unable to read cookie file {cookie_path}: {exc}")
    else:
        log.debug(f"Cookie file not found: {cookie_path}")

    # Support the old py_common config names as a fallback, without logging
    # their values. Environment variables are useful for container secrets.
    values.setdefault("fc2cmadb-session", _config_value("fc2cmadb_session"))
    values.setdefault("XSRF-TOKEN", _config_value("xsrf_token"))
    values.setdefault("ageVerified", _config_value("age_verified") or "true")

    overrides = {
        "fc2cmadb-session": os.environ.get("FC2CMADB_SESSION", ""),
        "XSRF-TOKEN": os.environ.get("FC2CMADB_XSRF_TOKEN", ""),
        "ageVerified": os.environ.get("FC2CMADB_AGE_VERIFIED", ""),
    }
    for name, value in overrides.items():
        if value:
            values[name] = value

    return {name: value for name, value in values.items() if value}


def _cookie_payload(cookies: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"name": name, "value": value, "domain": SITE_HOST, "path": "/"}
        for name, value in cookies.items()
    ]


def _set_cookie(session: requests.Session, name: str, value: str, **kwargs: Any) -> None:
    try:
        session.cookies.set(name, value, **kwargs)
    except (TypeError, ValueError):
        # Be tolerant of malformed domain/path metadata returned by a
        # FlareSolverr version while still retaining the cookie value.
        session.cookies.set(name, value)


def _new_session(solution: dict[str, Any] | None, cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    # Direct connections avoid accidentally routing fc2cmadb through a proxy
    # that may expose or alter the authenticated request.
    session.proxies = {}
    session.trust_env = False

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

    for name, value in cookies.items():
        _set_cookie(session, name, value, domain=f".{SITE_HOST}", path="/")

    return session


def _get_flaresolverr_solution(url: str, cookies: dict[str, str]) -> dict[str, Any] | None:
    """Return a FlareSolverr solution, or None when it is unavailable."""
    try:
        response = requests.post(
            FLARESOLVERR_URL,
            json={
                "cmd": "request.get",
                "url": url,
                "cookies": _cookie_payload(cookies),
                "session_ttl_minutes": 5,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning(f"Unable to contact FlareSolverr at {FLARESOLVERR_URL}: {exc}")
        return None

    if response.status_code != 200:
        log.warning(
            f"FlareSolverr returned HTTP {response.status_code} for {url}; trying a direct request"
        )
        return None

    try:
        body = response.json()
    except ValueError:
        log.warning(f"FlareSolverr returned invalid JSON for {url}; trying a direct request")
        return None

    if body.get("status") not in (None, "ok") or not isinstance(body.get("solution"), dict):
        log.warning(f"FlareSolverr did not return a usable solution for {url}: {body.get('message', 'unknown error')}")
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
_DEFAULT_WINDOW_SECONDS = 60


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
    remaining_str = ""
    reset_str = ""
    retry_str = ""
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "x-ratelimit-remaining":
            remaining_str = v
        elif k_lower == "x-ratelimit-reset":
            reset_str = v
        elif k_lower == "retry-after":
            retry_str = v

    if not remaining_str:
        return state

    try:
        remaining = int(remaining_str)
    except (TypeError, ValueError):
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

    if cooldown <= 0 and remaining <= 1:
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


def _failure_result(
    tag_name: str,
    *,
    details: str = "",
    url: str = "",
) -> ScrapedScene:
    """Return a scraped scene with a single sentinel tag and optional metadata.

    Preserves the scene's FC2 code (extracted from *url*) so the code is not
    wiped on failure.  *details* is appended to the code as a human-readable
    suffix.
    """
    result: ScrapedScene = {
        "tags": [{"name": tag_name}],
    }

    article_id = _article_id_from_url(url)
    if article_id:
        result["code"] = f"FC2-PPV-{article_id}"

    if details:
        result["details"] = details

    if url:
        result["urls"] = [url]

    return result


def scene_from_url(url: str) -> ScrapedScene:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {
        SITE_HOST,
        f"www.{SITE_HOST}",
    }:
        log.error(f"[fc2madb.py] FAILURE TYPE=unsupported_url  URL={url}")
        return _failure_result("FC2MADB: Parse Error", url=url)

    cookies = _load_cookies()
    if not cookies.get("fc2cmadb-session") or not cookies.get("XSRF-TOKEN"):
        log.error(
            f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  "
            f"missing fc2cmadb-session or XSRF-TOKEN. "
            f"Put a browser cookie export in {COOKIE_FILE} or set "
            "FC2CMADB_SESSION and FC2CMADB_XSRF_TOKEN."
        )
        return _failure_result("FC2MADB: Auth Error", url=url)

    # ---- Rate-limit: load state and wait if in cooldown ----
    rate_state = _load_rate_state()
    _wait_for_cooldown(rate_state)

    solution = _get_flaresolverr_solution(url, cookies)
    session = _new_session(solution, cookies)
    initial_html = str(solution.get("response", "")) if solution else ""

    # Track where headers came from for rate-limit state updates
    response_headers: dict[str, str] | None = None
    if solution and isinstance(solution.get("headers"), dict):
        response_headers = solution["headers"]

    try:
        if _login_page(initial_html):
            log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  login prompt in FlareSolverr response")
            return _failure_result("FC2MADB: Auth Error", url=url)
        if not _inertia_version(initial_html):
            initial = session.get(url, timeout=REQUEST_TIMEOUT)
            if initial.status_code == 403 and "1005" in initial.text:
                log.error(f"[fc2madb.py] FAILURE TYPE=cloudflare  URL={url}  ASN block")
                return _failure_result("FC2MADB: Cloudflare Blocked", url=url)
            if _login_page(initial.text, initial.url):
                log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  login prompt in direct fetch")
                return _failure_result("FC2MADB: Auth Error", url=url)
            initial_html = initial.text
            if response_headers is None:
                response_headers = dict(initial.headers)
    except requests.RequestException as exc:
        log.error(f"[fc2madb.py] FAILURE TYPE=unreachable  URL={url}  {exc}")
        return _failure_result("FC2MADB: Unreachable", details=str(exc), url=url)

    # ---- Parse Inertia page data from the initial HTML response ----
    # The initial HTML (from FlareSolverr or direct GET) contains the full
    # page data embedded in a <script data-page="app"> tag.  Extracting it
    # directly avoids a second GET request, halving our request count and
    # staying well within the site's aggressive X-RateLimit-Limit: 3 budget.
    props = _inertia_page_data(initial_html)
    if not props:
        # Fallback: make an Inertia JSON GET if the initial HTML didn't
        # contain parseable page data.
        version = _inertia_version(initial_html)
        if not version:
            log.error(
                f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  no Inertia version in HTML"
            )
            return _failure_result("FC2MADB: Parse Error", url=url)

        session.headers.update(
            {
                "X-Inertia": "true",
                "X-Requested-With": "XMLHttpRequest",
                "X-Inertia-Partial-Component": "Articles/Show",
                "X-Inertia-Partial-Data": "article,actresses",
                "X-Inertia-Version": version,
                "Referer": url,
                "Accept": "text/html, application/xhtml+xml",
                "Cache-Control": "no-cache",
            }
        )

        try:
            info_response = session.get(url, timeout=REQUEST_TIMEOUT)
            if _login_page(info_response.text, info_response.url):
                log.error(
                    f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  Inertia GET redirected to login"
                )
                return _failure_result("FC2MADB: Auth Error", url=url)
            if info_response.status_code != 200:
                log.error(
                    f"[fc2madb.py] FAILURE TYPE=http_{info_response.status_code}  URL={url}"
                )
                tag_name = (
                    "FC2MADB: Not Found"
                    if info_response.status_code == 404
                    else "FC2MADB: Rate Limited"
                    if info_response.status_code == 429
                    else "FC2MADB: Unreachable"
                )
                return _failure_result(
                    tag_name,
                    details=f"HTTP {info_response.status_code}",
                    url=url,
                )
            payload = info_response.json()
            props = payload.get("props", {}) if isinstance(payload, dict) else {}
            if response_headers is None:
                response_headers = dict(info_response.headers)
        except (requests.RequestException, ValueError) as exc:
            log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  {exc}")
            return _failure_result("FC2MADB: Parse Error", details=str(exc), url=url)

    # ---- Update rate-limit state from response headers ----
    if response_headers:
        _update_rate_state_from_headers(rate_state, response_headers)
        _save_rate_state(rate_state)
    article = props.get("article") if isinstance(props, dict) else None
    if not isinstance(article, dict):
        log.error(f"[fc2madb.py] FAILURE TYPE=not_found  URL={url}  article object missing")
        return _failure_result("FC2MADB: Not Found", url=url)

    scene: ScrapedScene = {}
    title = str(article.get("title") or "").strip()
    if title:
        scene["title"] = title

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

    actresses = props.get("actresses")
    if isinstance(actresses, list):
        scene["performers"] = [
            {"name": str(performer["name"]).strip()}
            for performer in actresses
            if isinstance(performer, dict) and performer.get("name")
        ]

    scene["urls"] = [url]
    return scene


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
