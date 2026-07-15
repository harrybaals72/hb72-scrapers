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

    solution = _get_flaresolverr_solution(url, cookies)
    session = _new_session(solution, cookies)
    initial_html = str(solution.get("response", "")) if solution else ""

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
    except requests.RequestException as exc:
        log.error(f"[fc2madb.py] FAILURE TYPE=unreachable  URL={url}  {exc}")
        return _failure_result("FC2MADB: Unreachable", details=str(exc), url=url)

    version = _inertia_version(initial_html)
    if not version:
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  no Inertia version in HTML")
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
            log.error(f"[fc2madb.py] FAILURE TYPE=auth  URL={url}  Inertia GET redirected to login")
            return _failure_result("FC2MADB: Auth Error", url=url)
        if info_response.status_code != 200:
            log.error(f"[fc2madb.py] FAILURE TYPE=http_{info_response.status_code}  URL={url}")
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
    except (requests.RequestException, ValueError) as exc:
        log.error(f"[fc2madb.py] FAILURE TYPE=parse_error  URL={url}  {exc}")
        return _failure_result("FC2MADB: Parse Error", details=str(exc), url=url)

    props = payload.get("props", {}) if isinstance(payload, dict) else {}
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
