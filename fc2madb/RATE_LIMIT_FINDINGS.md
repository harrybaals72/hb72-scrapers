# fc2cmadb.com Rate-Limit Investigation

## Summary

The site uses a Laravel throttle middleware with a limit of **3 requests per window**. The window duration is approximately **1–1.5 seconds**, not the 60-second default assumed by the scraper's heuristic. No `X-RateLimit-Reset` or `Retry-After` headers are provided on any response (200 or 429). On a 429, the `remember_web` cookie is deleted (Max-Age=0), which logs the user out of their browser session.

## Headers Observed

All 200 responses include:
- `X-RateLimit-Limit: 3`
- `X-RateLimit-Remaining: 2, 1, 0` (decrements per request within the window)

**Never observed:**
- `X-RateLimit-Reset`
- `Retry-After`

The 429 response carries **no rate-limit headers at all**.

## Window Behavior

Rapid-fire testing (0.0s delay between requests):

```
#1  HTTP 200  Remaining=2   ← window starts
#2  HTTP 200  Remaining=1
#3  HTTP 200  Remaining=0
#4  HTTP 429  Remaining=-   ← 4th request too fast, window hasn't reset
#5  HTTP 200  Remaining=2   ← window already reset
```

- Requests 1–3 took ~1.1s total.
- The 4th request arrived before the window reset (~0.9s from start), triggering 429.
- By request #5 (~0.2s after #4), the window had already reset.

**Actual window: ~1–1.5 seconds.**

## 429 Response Details

| Aspect | Value |
|---|---|
| Status | 429 |
| Rate-limit headers | None |
| Content-Type | `text/html; charset=utf-8` |
| Body size | ~24KB (Laravel error page) |
| Inertia component | `"Error"` (not `"Articles/Show"`) |
| Inertia props | `{"status": 429, "message": ""}` |
| Inertia version | `""` (empty string) |
| Set-Cookie (XSRF-TOKEN) | Refreshed with valid 2-hour expiry |
| Set-Cookie (fc2cmadb-session) | Refreshed with valid 2-hour expiry |
| Set-Cookie (remember_web) | **Deleted** — `Max-Age=0; expires=past date` |

## Problems with the Scraper's Rate-Limit Logic

### 1. 60-second heuristic cooldown is wildly wrong
`_update_rate_state_from_headers` sets a 60-second cooldown when `remaining <= 1` and no `X-RateLimit-Reset` is present. The actual window is ~1–1.5 seconds. This causes unnecessary multi-minute waits.

### 2. 429 is not detected in the primary code path
The direct GET branch in `scene_from_url` handles:
- 403/1005 → Cloudflare block
- Login page → Auth error
- Everything else → treated as success

A 429 falls into "everything else". The response body lacks parseable Inertia article data (the 429 page has `component: "Error"`), so it falls through to the Inertia fallback GET. That second GET also hits 429, wasting another rate-limit slot. The 429 is only classified in the fallback path's status code check — but by then an extra request was already made.

### 3. No rate-limit headers on the 429 response
Even when the 429 is detected, there are no `Retry-After` or `X-RateLimit-Reset` headers to know how long to wait. The window resets in ~1s regardless.

### 4. Session cookies are refreshed on 429 but not captured
The 429 response still sends fresh `XSRF-TOKEN` and `fc2cmadb-session` cookies. The scraper creates a fresh session per invocation from `cookies.json`, so it misses these refreshed tokens. The `remember_web` cookie is deleted (Max-Age=0), logging the user out of their browser.

### 5. Rate state persistence is fragile
`rate_state.json` coordinates across independent Stash scrape invocations, but:
- The server window (~1s) is much shorter than the file read/write cycle
- Saved `remaining` values may be stale by the next invocation
- Concurrent Stash tasks race on the same file

## Changes Applied (2026-07-16)

The scraper was updated with two changes:

### 1. `_DEFAULT_WINDOW_SECONDS`: 60 → 5
Reduced the heuristic cooldown window from 60 seconds to 5 seconds. This matches the observed server window (~1–1.5s) with a safety margin. After a request with `remaining ≤ 1`, the scraper waits up to 5 seconds before the next request.

### 2. 429 detection in primary direct-GET path
Added an `elif initial.status_code == 429:` check before the `else` (success) branch. On a 429, the scraper returns a `"FC2MADB: Rate Limited"` failure immediately **without**:
- Saving rate state (cookies are dead after a 429 — useless)
- Making a second Inertia GET (would waste another rate-limit slot)
- Touching the rate state file at all

### Not applied (reasoning)
- **Remaining ≤ 0 pre-flight check**: Not added. With a 5s cooldown, the window has always reset before the cooldown expires, so a stale `remaining=0` would cause false-positive "Rate Limited" returns.
- **Inertia component detection**: Not needed — the primary 429 check catches it before Inertia parsing is attempted.
- **File locking**: Not added. The 5s cooldown makes concurrent-window collisions very unlikely. If a race condition causes a 429, the new detection handles it cleanly.

## Recommended Fixes (not yet applied)

1. **Use the Inertia `component: "Error"` / `status: 429`** as an additional detection signal when parsing the response body.
2. **Optionally apply refreshed session cookies** from the response headers back into the session for subsequent requests within the same invocation.

## Test Methodology

A standalone Python script (`test_429_capture.py`) was used to send rapid back-to-back GET requests to `https://fc2cmadb.com/articles/4940229` using the same session (cookies from `cookies.json`). The 4th request within a ~1-second window triggered a 429. All response headers, body, inertia data, and session cookie state were captured for analysis. No FlareSolverr was used since the direct requests succeeded without Cloudflare blocking.
