# fc2cmadb.com Rate-Limit Investigation

## Summary

The site uses a Laravel throttle middleware with a limit of **3 requests per window**. The window duration is approximately **1–1.5 seconds**. No `X-RateLimit-Reset` or `Retry-After` headers are provided on any response (200 or 429). The scraper uses a **30-second** safety cooldown when `X-RateLimit-Remaining` is `0`, and a **5-second** heuristic for `X-RateLimit-Remaining` equal to `1` when no server reset is supplied. On a 429, the `remember_web` cookie is deleted (Max-Age=0), which logs the user out of their browser session.

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

## Current scraper behavior

- `_DEFAULT_WINDOW_SECONDS` is 5 seconds for a response with one request remaining when the server supplies no reset value; zero remaining uses a 30-second cooldown.
- Every direct, fallback, deferred, retry, and FlareSolverr-represented origin response is recorded immediately.
- 429 responses are transient: rate state is saved, `remaining` is forced to zero, a 30-second cooldown is forced, and the scraper returns an empty result.
- An advisory lock serializes rate-state coordination and FC2MADB requests across scraper processes.

## Test Methodology

A standalone Python script (`test_429_capture.py`) was used to send rapid back-to-back GET requests to `https://fc2cmadb.com/articles/4940229` using the same session (cookies from `cookies.json`). The 4th request within a ~1-second window triggered a 429. All response headers, body, inertia data, and session cookie state were captured for analysis. No FlareSolverr was used since the direct requests succeeded without Cloudflare blocking.
