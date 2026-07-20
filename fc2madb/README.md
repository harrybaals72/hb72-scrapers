# FC2MADB Stash scraper

Copy `fc2madb.py` and `fc2madb.yml` into Stash's `scrapers` directory, then reload scrapers in Stash. Browser cookie exports are not used.

## Configuration

Create the local `config.ini` used by `py_common` beside `fc2madb.py` and keep it private:

```ini
fc2cmadb_email = your-login-email
fc2cmadb_password = your-login-password
```

`config.ini` is ignored by Git and is not included in the scraper manifest. Both values are required for the initial login or session renewal; a valid saved session can be reused without them. Their values are never logged.

## Login and rate limiting

The scraper uses FlareSolverr at `http://9.9.9.200:8191/v1` by default to render the login page and obtain a Cloudflare Turnstile token. It then posts the credentials directly to `https://fc2cmadb.com/login` over HTTPS and checks the authenticated Inertia response before scraping.

After a successful authenticated article response, the scraper stores its own canonicalized session cookies in the private, ignored `auth_session.json` beside the script. Later runs try that session first and skip FlareSolverr/login when `auth.user` is present. If the saved session is missing, expired, redirected to `/login`, or fails the authenticated-props check, the scraper performs one credential login and retries; it never treats an unverified article response as authenticated. This is not a browser-cookie import: the file is created and refreshed only by this scraper, written atomically with mode `0600`, and cookie values are never logged.

Override the FlareSolverr endpoint with `FLARESOLVERR_URL`. Advanced deployment tuning is available through `FC2CMADB_TIMEOUT`, `FC2CMADB_FLARESOLVERR_TIMEOUT_MS`, `FC2CMADB_TURNSTILE_WIDGET_WAIT_SECONDS`, and `FC2CMADB_TURNSTILE_TAB_COUNT` (default: `8`).

The existing cross-process rate-state lock and cooldown remain active for login and article requests. A 429 is terminal for that scrape, forces and persists a 30-second cooldown even when the response omits rate-limit headers, and prevents an immediate login retry. The next invocation waits before making another request.

Authentication diagnostics are logged as `AUTH stage=...` records. They report only response status, redirect path, cookie-category counts, and whether the saved session was reused; credentials, Turnstile/XSRF values, cookie values, and dynamic remember-cookie names are never logged. `mode=credentials browser_cookie_import=disabled` confirms that the active scraper is not using the former browser-cookie method.

Rate-limit diagnostics include `phase`, `source`, `status`, and observed header names. For example, `source=flaresolverr_solution` means FlareSolverr returned no forwarded `X-RateLimit-Remaining`; `source=origin_response` means the direct fc2cmadb response omitted it. These are informational and do not indicate failure by themselves.
