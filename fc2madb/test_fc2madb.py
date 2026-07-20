import importlib.util
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


# The scraper is loaded with the small py_common interface supplied by Stash.
log = logging.getLogger("fc2madb-test")
py_common = types.ModuleType("py_common")
py_common.log = log
types_module = types.ModuleType("py_common.types")
types_module.ScrapedScene = dict
util_module = types.ModuleType("py_common.util")
util_module.scraper_args = lambda: ("", {})
sys.modules.update(
    {
        "py_common": py_common,
        "py_common.types": types_module,
        "py_common.util": util_module,
    }
)

spec = importlib.util.spec_from_file_location("fc2madb_under_test", Path(__file__).with_name("fc2madb.py"))
fc2madb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc2madb)


URL = "https://fc2cmadb.com/articles/4604611"


def initial_html(*, user=True, article=True, status=200, title="Article title", description="Article description"):
    props = {"auth": {"user": {"id": 1} if user else None}}
    if article:
        props["article"] = {
            "title": title,
            "description": description,
            "video_id": "123",
        }
    if status != 200:
        props["status"] = status
    page = {"version": "version-1", "component": "Articles/Show", "props": props}
    return '<script data-page="app">' + json.dumps(page) + "</script>"


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None, headers=None, url=URL):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = headers or {"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "2"}
        self.url = url

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeCookies:
    def __init__(self):
        self.values = {}

    def set(self, name, value, **kwargs):
        self.values[name] = value

    def __iter__(self):
        return iter(())

    def clear(self):
        self.values.clear()


class FakeSession:
    def __init__(self, responses, post_responses=None):
        self.responses = list(responses)
        self.post_responses = list(post_responses or [])
        self.calls = []
        self.post_calls = []
        self.headers = {}
        self.cookies = FakeCookies()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_responses:
            raise AssertionError("unexpected POST")
        response = self.post_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.rate_file = str(Path(self.tempdir.name) / "rate_state.json")
        self.cookies = {"fc2cmadb-session": "session", "XSRF-TOKEN": "token"}

    def tearDown(self):
        self.tempdir.cleanup()

    def run_scrape(self, responses):
        session = FakeSession(responses)
        with patch.object(fc2madb, "RATE_LIMIT_STATE_FILE", self.rate_file), patch.object(
            fc2madb, "_credentials", return_value=("email", "password")
        ), patch.object(fc2madb, "_login_with_credentials", return_value=session), patch.object(
            fc2madb.requests, "Session", return_value=session
        ), patch.object(
            fc2madb, "_get_flaresolverr_solution", side_effect=AssertionError("FlareSolverr should not run")
        ):
            result = fc2madb.scene_from_url(URL)
        return result, session

    def test_credential_login_uses_turnstile_solution_for_direct_https_post(self):
        warm_solution = {
            "headers": {"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "2"},
        }
        solved_solution = {
            "headers": {"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "2"},
            "userAgent": "FlareSolverr test agent",
            "turnstile_token": "test-turnstile-token",
            "cookies": [{"name": "XSRF-TOKEN", "value": "encoded%3Dtoken"}],
        }
        login_response = FakeResponse(
            status_code=409,
            headers={"X-Inertia-Location": "https://fc2cmadb.com"},
        )
        session = FakeSession([], [login_response])
        commands = [
            {"status": "ok"},
            {"status": "ok", "solution": warm_solution},
            {"status": "ok", "solution": solved_solution},
            {"status": "ok"},
        ]
        with patch.object(fc2madb, "_flaresolverr_command", side_effect=commands) as command, patch.object(
            fc2madb, "_new_session", return_value=session
        ), patch.object(fc2madb.time, "sleep"), patch.object(
            fc2madb, "RATE_LIMIT_STATE_FILE", self.rate_file
        ):
            result = fc2madb._login_with_credentials({}, "email", "password")
        self.assertIs(result, session)
        self.assertEqual(len(session.post_calls), 1)
        post_url, post_kwargs = session.post_calls[0]
        self.assertEqual(post_url, fc2madb.LOGIN_URL)
        self.assertEqual(
            post_kwargs["json"],
            {
                "email": "email",
                "password": "password",
                "remember": True,
                "token": "test-turnstile-token",
            },
        )
        self.assertEqual(post_kwargs["headers"]["X-XSRF-TOKEN"], "encoded=token")
        self.assertNotIn("X-Inertia", session.headers)
        self.assertNotIn("X-XSRF-TOKEN", session.headers)
        self.assertEqual(command.call_args_list[-1].args[0]["cmd"], "sessions.destroy")

    def test_login_cookie_canonicalization_prefers_response_values(self):
        session = requests.Session()
        session.cookies.set(
            "fc2cmadb-session", "stale", domain=".fc2cmadb.com", path="/"
        )
        session.cookies.set(
            "XSRF-TOKEN", "stale-xsrf", domain=".fc2cmadb.com", path="/"
        )
        response_cookies = requests.cookies.RequestsCookieJar()
        response_cookies.set(
            "fc2cmadb-session", "fresh", domain="fc2cmadb.com", path="/"
        )
        response_cookies.set(
            "XSRF-TOKEN", "fresh-xsrf", domain="fc2cmadb.com", path="/"
        )
        for cookie in response_cookies:
            session.cookies.set_cookie(cookie)

        values = fc2madb._canonicalize_session_cookies(
            session, response_cookies
        )

        self.assertEqual(values["fc2cmadb-session"], "fresh")
        self.assertEqual(values["XSRF-TOKEN"], "fresh-xsrf")
        matching = [
            cookie
            for cookie in session.cookies
            if cookie.name in {"fc2cmadb-session", "XSRF-TOKEN"}
        ]
        self.assertEqual(len(matching), 2)
        self.assertTrue(all(cookie.domain == fc2madb.SITE_HOST for cookie in matching))

    def test_new_session_collapses_duplicate_solution_cookie_names(self):
        solution = {
            "cookies": [
                {"name": "XSRF-TOKEN", "value": "stale", "domain": ".fc2cmadb.com"},
                {"name": "XSRF-TOKEN", "value": "fresh", "domain": "fc2cmadb.com"},
            ]
        }
        session = fc2madb._new_session(solution)
        matching = [
            cookie for cookie in session.cookies if cookie.name == "XSRF-TOKEN"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].value, "fresh")
        self.assertEqual(matching[0].domain, fc2madb.SITE_HOST)

    def test_login_success_rejects_redirect_back_to_login(self):
        self.assertFalse(
            fc2madb._login_succeeded(
                FakeResponse(status_code=302, headers={"Location": fc2madb.LOGIN_URL})
            )
        )
        self.assertTrue(
            fc2madb._login_succeeded(
                FakeResponse(status_code=302, headers={"Location": "/dashboard"})
            )
        )
        self.assertFalse(
            fc2madb._login_succeeded(
                FakeResponse(
                    status_code=409,
                    headers={"X-Inertia-Location": "https://fc2cmadb.com.evil.example/"},
                )
            )
        )

    def test_missing_credentials_never_contacts_login_services(self):
        with patch.object(fc2madb, "_credentials", return_value=("", "")), patch.object(
            fc2madb, "_login_with_credentials", side_effect=AssertionError("login should not run")
        ):
            self.assertEqual(fc2madb.scene_from_url(URL), {})

    def test_supported_url_secrets_are_removed_before_logging(self):
        supplied = (
            "https://user:PASSWORD_SECRET@fc2cmadb.com/articles/4604611"
            "?token=TOKEN_SECRET#COOKIE_SECRET"
        )
        with patch.object(fc2madb, "_credentials", return_value=("", "")), self.assertLogs(
            log, level="ERROR"
        ) as captured:
            self.assertEqual(fc2madb.scene_from_url(supplied), {})
        output = "\n".join(captured.output)
        self.assertIn(URL, output)
        self.assertNotIn("PASSWORD_SECRET", output)
        self.assertNotIn("TOKEN_SECRET", output)
        self.assertNotIn("COOKIE_SECRET", output)

    def test_flaresolverr_error_logging_suppresses_endpoint_secrets(self):
        endpoint = "http://user:PASSWORD_SECRET@9.9.9.200:8191/v1?token=TOKEN_SECRET"
        with patch.object(fc2madb, "FLARESOLVERR_URL", endpoint), patch.object(
            fc2madb.requests,
            "post",
            side_effect=requests.ConnectionError("COOKIE_SECRET"),
        ), self.assertLogs(log, level="WARNING") as captured:
            self.assertIsNone(fc2madb._flaresolverr_command({"cmd": "sessions.create"}))
        output = "\n".join(captured.output)
        self.assertIn("9.9.9.200:8191/v1", output)
        self.assertNotIn("PASSWORD_SECRET", output)
        self.assertNotIn("TOKEN_SECRET", output)
        self.assertNotIn("COOKIE_SECRET", output)

    def test_normal_success_requires_two_origin_gets_and_keeps_title_in_details(self):
        deferred = {
            "component": "Articles/Show",
            "props": {"actresses": [{"name": "Performer"}]},
        }
        result, session = self.run_scrape(
            [
                FakeResponse(text=initial_html()),
                FakeResponse(payload=deferred),
            ]
        )
        self.assertEqual(len(session.calls), 2)
        self.assertNotIn("title", result)
        self.assertEqual(result["details"], "Article title\nArticle description")
        self.assertEqual(result["performers"], [{"name": "Performer"}])

    def test_authenticated_404_is_only_sentinel_tag(self):
        response = FakeResponse(status_code=404, text=initial_html(status=404))
        result, session = self.run_scrape([response])
        self.assertEqual(result, {"tags": [{"name": "FC2MADB 404"}]})
        self.assertEqual(len(session.calls), 1)

    def test_unauthenticated_404_does_not_tag_scene(self):
        response = FakeResponse(status_code=404, text=initial_html(user=False, status=404))
        result, _ = self.run_scrape([response])
        self.assertEqual(result, {})

    def test_missing_article_is_transient_empty_result(self):
        result, _ = self.run_scrape(
            [FakeResponse(text=initial_html(article=False))]
        )
        self.assertEqual(result, {})

    def test_zero_remaining_header_persists_30_second_cooldown(self):
        response = FakeResponse(
            status_code=404,
            text=initial_html(status=404),
            headers={"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "0"},
        )
        result, session = self.run_scrape([response])
        self.assertEqual(result, {"tags": [{"name": "FC2MADB 404"}]})
        self.assertEqual(len(session.calls), 1)
        state = json.loads(Path(self.rate_file).read_text())
        cooldown = state["cooldown_until"] - time.time()
        self.assertGreaterEqual(cooldown, 29)
        self.assertLessEqual(cooldown, 30.5)

    def test_deferred_429_is_saved_and_never_returns_partial_metadata(self):
        deferred_429 = FakeResponse(status_code=429, text="rate limited", headers={})
        result, session = self.run_scrape(
            [FakeResponse(text=initial_html()), deferred_429]
        )
        self.assertEqual(result, {})
        self.assertEqual(len(session.calls), 2)
        state = json.loads(Path(self.rate_file).read_text())
        self.assertEqual(state["remaining"], 0)
        cooldown = state["cooldown_until"] - time.time()
        self.assertGreaterEqual(cooldown, 29)
        self.assertLessEqual(cooldown, 30.5)

    def test_flaresolverr_404_uses_authenticated_props_status(self):
        solution_html = initial_html(user=True, article=False, status=404)
        solution = {
            "response": solution_html,
            "headers": {
                "X-RateLimit-Limit": "3",
                "X-RateLimit-Remaining": "1",
            },
            # Deliberately omit status: this is common in FlareSolverr output.
        }
        session = FakeSession([FakeResponse(status_code=403, text="Cloudflare 1005")])
        with patch.object(fc2madb, "RATE_LIMIT_STATE_FILE", self.rate_file), patch.object(
            fc2madb, "_credentials", return_value=("email", "password")
        ), patch.object(fc2madb, "_login_with_credentials", return_value=session), patch.object(
            fc2madb.requests, "Session", return_value=session
        ), patch.object(
            fc2madb, "_get_flaresolverr_solution", return_value=solution
        ):
            result = fc2madb.scene_from_url(URL)
        self.assertEqual(result, {"tags": [{"name": "FC2MADB 404"}]})
        state = json.loads(Path(self.rate_file).read_text())
        self.assertEqual(state["limit"], 3)
        self.assertEqual(state["remaining"], 1)

    def test_flaresolverr_429_saves_cooldown(self):
        solution = {
            "status": 429,
            "response": "rate limited",
            "headers": {},
        }
        session = FakeSession([FakeResponse(status_code=403, text="Cloudflare 1005")])
        with patch.object(fc2madb, "RATE_LIMIT_STATE_FILE", self.rate_file), patch.object(
            fc2madb, "_credentials", return_value=("email", "password")
        ), patch.object(fc2madb, "_login_with_credentials", return_value=session), patch.object(
            fc2madb.requests, "Session", return_value=session
        ), patch.object(
            fc2madb, "_get_flaresolverr_solution", return_value=solution
        ):
            result = fc2madb.scene_from_url(URL)
        self.assertEqual(result, {})
        state = json.loads(Path(self.rate_file).read_text())
        self.assertEqual(state["remaining"], 0)
        cooldown = state["cooldown_until"] - time.time()
        self.assertGreaterEqual(cooldown, 29)
        self.assertLessEqual(cooldown, 30.5)
        self.assertEqual(len(session.calls), 1)

    def test_direct_request_exception_does_not_invoke_flaresolverr(self):
        result, session = self.run_scrape([requests.ReadTimeout("read timed out")])
        self.assertEqual(result, {})
        self.assertEqual(len(session.calls), 1)

    def test_empty_actresses_list_is_a_success(self):
        deferred = {"component": "Articles/Show", "props": {"actresses": []}}
        result, _ = self.run_scrape(
            [FakeResponse(text=initial_html()), FakeResponse(payload=deferred)]
        )
        self.assertEqual(result["performers"], [])

    def test_title_only_stays_in_details(self):
        deferred = {"component": "Articles/Show", "props": {"actresses": []}}
        result, _ = self.run_scrape(
            [
                FakeResponse(text=initial_html(description="")),
                FakeResponse(payload=deferred),
            ]
        )
        self.assertEqual(result["details"], "Article title")
        self.assertNotIn("title", result)

    def test_empty_shell_uses_full_inertia_fallback(self):
        shell = '<script data-page="app">' + json.dumps({"version": "version-1"}) + "</script>"
        fallback = {
            "component": "Articles/Show",
            "props": {
                "auth": {"user": {"id": 1}},
                "article": {"title": "Fallback title"},
            },
        }
        deferred = {"component": "Articles/Show", "props": {"actresses": []}}
        result, session = self.run_scrape(
            [FakeResponse(text=shell), FakeResponse(payload=fallback), FakeResponse(payload=deferred)]
        )
        self.assertEqual(result["details"], "Fallback title")
        self.assertEqual(len(session.calls), 3)

    def test_deferred_409_retry_records_retry_response(self):
        version_change = '<script data-page="app">' + json.dumps({"version": "version-2"}) + "</script>"
        deferred = {"component": "Articles/Show", "props": {"actresses": []}}
        result, session = self.run_scrape(
            [
                FakeResponse(text=initial_html()),
                FakeResponse(status_code=409, text=version_change),
                FakeResponse(payload=deferred),
            ]
        )
        self.assertEqual(result["performers"], [])
        self.assertEqual(len(session.calls), 3)


if __name__ == "__main__":
    unittest.main()
