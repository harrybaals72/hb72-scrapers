import importlib.util
import json
import logging
import sys
import tempfile
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


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
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
            fc2madb, "_load_cookies", return_value=self.cookies
        ), patch.object(fc2madb.requests, "Session", return_value=session), patch.object(
            fc2madb, "_get_flaresolverr_solution", side_effect=AssertionError("FlareSolverr should not run")
        ):
            result = fc2madb.scene_from_url(URL)
        return result, session

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

    def test_deferred_429_is_saved_and_never_returns_partial_metadata(self):
        deferred_429 = FakeResponse(status_code=429, text="rate limited", headers={})
        result, session = self.run_scrape(
            [FakeResponse(text=initial_html()), deferred_429]
        )
        self.assertEqual(result, {})
        self.assertEqual(len(session.calls), 2)
        state = json.loads(Path(self.rate_file).read_text())
        self.assertEqual(state["remaining"], 0)
        self.assertGreater(state["cooldown_until"], 0)

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
            fc2madb, "_load_cookies", return_value=self.cookies
        ), patch.object(fc2madb.requests, "Session", return_value=session), patch.object(
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
            fc2madb, "_load_cookies", return_value=self.cookies
        ), patch.object(fc2madb.requests, "Session", return_value=session), patch.object(
            fc2madb, "_get_flaresolverr_solution", return_value=solution
        ):
            result = fc2madb.scene_from_url(URL)
        self.assertEqual(result, {})
        state = json.loads(Path(self.rate_file).read_text())
        self.assertEqual(state["remaining"], 0)
        self.assertGreater(state["cooldown_until"], 0)
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
