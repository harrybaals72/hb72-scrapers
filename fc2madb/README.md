# FC2MADB Stash scraper

Copy `fc2madb.py` and `fc2madb.yml` into Stash's `scrapers` directory, then reload scrapers in Stash. Browser cookie exports are not used.

## Configuration

Create the local `config.ini` used by `py_common` beside `fc2madb.py` and keep it private:

```ini
fc2cmadb_email = your-login-email
fc2cmadb_password = your-login-password
```

`config.ini` is ignored by Git and is not included in the scraper manifest. The scraper requires both values; it never logs their values.

## Login and rate limiting

The scraper uses FlareSolverr at `http://9.9.9.200:8191/v1` by default to render the login page and obtain a Cloudflare Turnstile token. It then posts the credentials directly to `https://fc2cmadb.com/login` over HTTPS and checks the authenticated Inertia response before scraping.

Override the FlareSolverr endpoint with `FLARESOLVERR_URL`. Advanced deployment tuning is available through `FC2CMADB_TIMEOUT`, `FC2CMADB_FLARESOLVERR_TIMEOUT_MS`, `FC2CMADB_TURNSTILE_WIDGET_WAIT_SECONDS`, and `FC2CMADB_TURNSTILE_TAB_COUNT` (default: `8`).

The existing cross-process rate-state lock and cooldown remain active for login and article requests. A 429 is terminal for that scrape and triggers the existing 30-second safety cooldown.
