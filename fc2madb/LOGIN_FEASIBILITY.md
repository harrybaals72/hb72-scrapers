# Credential-Based Login Feasibility for fc2cmadb.com

## Conclusion

**Credential-based login is feasible with the official FlareSolverr v3.5.0 image currently running at `9.9.9.200:8191`.** The prior conclusion that the running image did not contain Turnstile support was incorrect.

A live test on 2026-07-20 established this working split:

1. Use a persistent FlareSolverr session to render the login page and solve Turnstile with `request.get` plus `tabs_till_verify`.
2. Copy the returned cookies and exact `userAgent` into a normal `requests.Session`.
3. Send the credentials and returned Turnstile token directly to `https://fc2cmadb.com/login` over HTTPS.

The successful login response issued a new `remember_web_*` cookie and an Inertia location redirect to the site home page. No credential or cookie values are recorded in this document.

This removes the need for repeated browser cookie exports. The credential flow is implemented in `fc2madb.py` on the `feat/fc2madb-credential-login` branch.

---

## Corrections to the Previous Report

### 1. FlareSolverr v3.5.0 does include PR #1634

[FlareSolverr PR #1634](https://github.com/FlareSolverr/FlareSolverr/pull/1634) was merged into `master` on 2025-12-03 as commit [`9f8c711`](https://github.com/FlareSolverr/FlareSolverr/commit/9f8c71131f44b77b1e1969862ccc0c9d69f8e742).

The merge commit is an ancestor of both current `master` and the `v3.5.0` tag. The official v3.5.0 source contains:

- `tabs_till_verify` on `request.get`
- detection of `input[name='cf-turnstile-response']`
- Tab/Space keyboard interaction
- `solution.turnstile_token`

The running service reports:

```json
{
  "msg": "FlareSolverr is ready!",
  "version": "3.5.0"
}
```

Docker Hub's `latest` and `v3.5.0` tags also had the same manifest digest when checked on 2026-07-20. Rebuilding from `master` is therefore **not required merely to obtain PR #1634**.

### 2. `"Challenge not detected!"` does not mean Turnstile support is absent

FlareSolverr uses that message for its normal Cloudflare challenge detector. A request that successfully returned a non-empty `turnstile_token` still reported `"Challenge not detected!"`.

The token itself—not that message—is the relevant result.

### 3. The Turnstile input is dynamically rendered

The raw server HTML for `/login` did not contain `cf-turnstile-response`. The input appeared in FlareSolverr's rendered DOM after the React/Inertia page initialized.

PR #1634 checks for the selector immediately after navigation and does not wait for it. Consequently, a fresh call such as this can return no token even though the input appears in the final rendered response:

```json
{
  "cmd": "request.get",
  "url": "https://fc2cmadb.com/login",
  "tabs_till_verify": 8
}
```

This timing race explains the earlier `turnstile_token: null` results better than the claim that v3.5.0 lacked the merged code.

### 4. `request.post` is not the recommended login mechanism

PR #1634 supports `tabs_till_verify` only for `request.get`. It does not add general form-filling automation, and FlareSolverr's README explicitly says the option is GET-only.

In a live test, FlareSolverr `request.post` after obtaining a token navigated to the home page but did not return an authenticated page or a `remember_web_*` cookie. By contrast, a direct Inertia POST using the FlareSolverr cookies, user agent, and token did issue the remember cookie.

Use FlareSolverr as the browser/Turnstile solver, then perform the credential POST with `requests`.

### 5. Historical cookie-loader finding

The previous cookie-based implementation loaded every record from `cookies.json`, including `remember_web_*`; it did not filter the jar down to only session and XSRF cookies. A live remember-only request confirmed that Laravel could use that cookie to issue a fresh session.

That finding explains the old implementation but is not part of the credential-based runtime. The current scraper has no `_load_cookies()` function, does not read `cookies.json`, and does not accept browser-cookie environment overrides. It uses only cookies obtained from its own FlareSolverr/credential flow, and may persist the canonicalized authenticated jar in its private `auth_session.json` for later reuse.

---

## Verified Login Flow

### Site-side fields

The current login bundle initializes and submits these fields:

- `email`
- `password`
- `remember`
- `token` — the Turnstile response

The browser client blocks submission when `token` is empty. The production Turnstile site key remains `0x4AAAAAADXOhIQVuvaNOgcG`.

The previous report described `_token` as a required form field. The current Inertia client does not include `_token` in its form data. It relies on the `XSRF-TOKEN` cookie and Axios's `X-XSRF-TOKEN` header. A scraper implementation should mirror that behavior by URL-decoding the cookie value for the header.

### Working FlareSolverr sequence

The following sequence worked against the running v3.5.0 container:

1. `sessions.create`
2. Warm-up `request.get` for `https://fc2cmadb.com/login` using that session
3. Wait for the rendered widget
4. A second `request.get` in the same session for `https://fc2cmadb.com/login#flaresolverr-turnstile` with `tabs_till_verify: 8`
5. Read `solution.turnstile_token`, `solution.cookies`, and `solution.userAgent`
6. Destroy the FlareSolverr session in a `finally` block after login is complete

The hash-only URL change is intentional. It keeps the already-rendered page in place so PR #1634 can see the dynamically inserted hidden input immediately, avoiding the fresh-navigation selector race.

`tabs_till_verify: 8` matched the login page's current keyboard order and returned a 752-character token in the successful test. This number is inherently brittle: navigation/layout changes can alter the required count, and a wrong count generally runs until `maxTimeout`.

### Credential POST

After solving:

- Create a direct `requests.Session` with `trust_env = False`.
- Install all FlareSolverr solution cookies with their domain/path metadata.
- Use exactly `solution.userAgent`.
- Send the credential POST directly to fc2cmadb over HTTPS, not to the HTTP FlareSolverr API.
- Include `X-Inertia: true`, `X-Requested-With: XMLHttpRequest`, `Referer`, and a URL-decoded `X-XSRF-TOKEN` header.
- Send JSON fields `email`, `password`, `remember: true`, and `token`.

The live credential test returned HTTP 409 with `X-Inertia-Location: https://fc2cmadb.com` and issued fresh session, XSRF, and `remember_web_*` cookies. An Inertia location response uses 409 to direct the client to a full-page visit; the newly issued remember cookie confirms that authentication with “remember me” succeeded.

---

## What This Means for 429 Logouts

The existing rate-limit investigation observed that a 429 response tells the receiving client to delete `remember_web_*`. That deletes the cookie from that client's cookie jar; it does not make manual browser export the only possible recovery path.

The credential-based scraper keeps the active login cookies in memory and, after authenticated article props are proven, atomically refreshes its private `auth_session.json`. If a 429 deletes `remember_web_*`, that scrape terminates and the persisted rate state enforces the existing 30-second safety cooldown; the damaged session is not trusted until a later article response proves authentication again. A later invocation reuses the saved session when valid, otherwise creates a fresh remembered session from credentials; no browser cookie export is involved.

The implementation retains the existing rate-limit lock/cooldown and never attempts credential login as an immediate retry after a 429.

---

## Implemented Credential Login

The scraper now reads `fc2cmadb_email` and `fc2cmadb_password` from its private `config.ini`; it no longer reads browser cookies or accepts cookie environment-variable overrides.

For each scrape it:

1. Acquires the existing cross-process rate-state lock and honors any saved cooldown.
2. Creates a FlareSolverr session, warms `/login`, then uses the hash-navigation Turnstile flow.
3. Records FlareSolverr-carried rate-limit headers, waits again when needed, and posts the credentials directly to fc2cmadb over HTTPS.
4. Requires Laravel/Inertia's successful redirect response before scraping.
5. Reuses that authenticated session for the article and deferred-performer requests, retaining the existing authenticated-props checks and 429 handling.
6. Saves canonicalized session cookies only after authenticated article props are proven, then destroys the temporary FlareSolverr session.
7. Destroys the FlareSolverr session in a `finally` block.

Credentials and returned Turnstile tokens are never persisted or logged. The scraper-created cookie session is persisted only in ignored `auth_session.json`, atomically with mode `0600`; browser cookie exports and `cookies.json` are never read. `config.ini` is Git-ignored and intentionally absent from the package manifest.

### Operational cautions

- The PR's Tab-count method is UI-order dependent and may break when the login page changes.
- The upstream PR's logging behavior should be checked when upgrading FlareSolverr. The scraper itself never logs credentials, Turnstile tokens, or cookie values; FlareSolverr logs should still be protected.
- The FlareSolverr endpoint is plain HTTP and normally unauthenticated. Restrict port 8191 to the trusted LAN/firewall. Sending credentials directly to fc2cmadb over HTTPS avoids exposing them to that API.
- Avoid CAPTCHA-solving services unless this UI-driven method becomes unreliable; they are not currently necessary.

---

## Evidence and Test Record

Checked on 2026-07-20; the resulting credential flow is implemented in `fc2madb.py`:

| Check | Result |
|---|---|
| FlareSolverr service at `9.9.9.200:8191` | Ready; v3.5.0 |
| PR #1634 merge state | Merged into `master` on 2025-12-03 |
| PR merge included in v3.5.0 | Yes |
| Official Docker `latest` versus `v3.5.0` | Same manifest digest at check time |
| Raw `/login` HTML | `Auth/Login`; Turnstile input not yet rendered |
| FlareSolverr-rendered `/login` | Turnstile input present |
| Fresh `tabs_till_verify` request | Intermittently misses dynamically rendered selector |
| Warm session + hash navigation + `tabs_till_verify: 8` | Returned non-empty Turnstile token |
| FlareSolverr `request.post` credential submission | Did not establish confirmed authentication |
| Direct HTTPS Inertia POST with FlareSolverr solution | Login accepted; new `remember_web_*` issued |
| Existing `remember_web_*` alone | `302 /dashboard`; fresh session/XSRF issued |

Primary upstream references:

- [PR #1634](https://github.com/FlareSolverr/FlareSolverr/pull/1634)
- [PR merge commit](https://github.com/FlareSolverr/FlareSolverr/commit/9f8c71131f44b77b1e1969862ccc0c9d69f8e742)
- [v3.5.0 release](https://github.com/FlareSolverr/FlareSolverr/releases/tag/v3.5.0)
- [v3.5.0 request API documentation](https://github.com/FlareSolverr/FlareSolverr/blob/v3.5.0/README.md#commands)
- [v3.5.0 Turnstile implementation](https://github.com/FlareSolverr/FlareSolverr/blob/v3.5.0/src/flaresolverr_service.py)
