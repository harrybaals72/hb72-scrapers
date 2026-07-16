# FC2MADB scraper review feedback

## Scope and verified behavior

The current working-tree version was reviewed against these required invariants:

1. Return scene fields only after an authenticated, successful scrape.
2. The sole failure result that may modify a scene is an **authenticated, verified page-not-found (404)** result, which returns only `{"tags": [{"name": "FC2MADB 404"}]}`.
3. The article title belongs in `details`, never the scene `title`; an article description follows it in `details` when present.
4. Every FC2MADB response that contains rate-limit headers updates persisted rate state; every 429 establishes a cooldown even though it has no such headers.

A live, rate-spaced run with the supplied valid cookies confirmed that the current scraper returns one performer for article `4604611` and none for `4940229`. Both successful results had `details` and no `title` field. The local cache also records that `actresses` is an Inertia deferred prop: the initial page has the article but not actresses, and a partial Inertia request returns the performer list. Therefore, **two FC2MADB article requests are required for a complete successful scrape** (one initial page request and one partial-data request).

## Required changes

### 1. Make authentication precede every 404 tag decision

**Problem**

- `scene_from_url()` returns the 404 tag immediately for a direct HTTP 404 (`fc2madb.py:512-531`), before parsing and validating `props.auth.user` (`:655-667`).
- The full Inertia fallback does the same for its HTTP 404 (`:611-637`).
- An authenticated 200 response with an unexpected/missing `article` object also returns the 404 tag (`:669-682`), despite not being an HTTP 404.

This can incorrectly tag a valid scene when FC2MADB uses a 404/error page for an expired or otherwise invalid session. It also treats a site/schema regression as terminal.

**Change**

Centralize page-result classification after parsing the Inertia payload:

1. Capture the actual HTTP status (or the Inertia `props.status` if the FlareSolverr fallback is the only page body available).
2. Parse page props and validate `auth` is a dict and `auth.user` is a dict.
3. If auth cannot be proved, return `{}` for **all** statuses, including 404.
4. Only when auth is proven and the page is explicitly 404, return exactly:
   ```python
   {"tags": [{"name": "FC2MADB 404"}]}
   ```
5. For every other non-200 result, malformed payload, unexpected Inertia component, or missing/non-dict `article` on a 200, log the reason and return `{}`. Do not infer a 404 from a missing article.

Keep the 404 result free of `code`, `details`, `urls`, and every other scene field.

### 2. Fail closed when the deferred performer request is unsuccessful

**Problem**

The deferred actresses request currently warns on a non-200 response, JSON error, no version, or missing actresses value (`fc2madb.py:684-740`) and then still builds the scene (`:752-794`). For example, a 429 or 500 on that request returns article metadata without performers. This violates the no-partial-update invariant and can cause Stash to apply incomplete data.

**Change**

Treat the full scrape as successful only when all of the following are true:

- the initial page proved `auth.user` and yielded a valid article;
- the partial Inertia request is 200 and is the expected `Articles/Show` response (not a login/error page);
- its JSON parses; and
- `props["actresses"]` is a list. An empty list is valid and means no performers.

If any of those checks fail, record rate state for the response first, then return `{}`. Do not build a partial scene. For a failure on this second request, return `{}` rather than a 404 tag: the initial request already established that the page existed, while a later error can be transient or authentication-related.

The existing title/details construction is correct and should be retained:

```python
# Never assign scene["title"].
# details = title, or f"{title}\n{description}" when both exist.
```

### 3. Record rate state from every response immediately, including all 429 paths

**Problem**

- The full Inertia fallback preserves the first response headers when `response_headers` is already set (`fc2madb.py:599-603`). Thus a fallback 429 (which has no rate headers) is not recognized as a 429 for cooldown purposes; stale initial headers are saved instead.
- The actresses request overwrites the header variable with the 429's empty headers (`:700-704`), and the final truthiness check (`:742-750`) can skip saving state entirely.
- More generally, a final `response_headers` variable cannot meet the requirement that *every* response with rate headers updates the rate-state file.

**Change**

Replace deferred/final header processing with a single helper, conceptually:

```python
def record_rate_response(state, response):
    _update_rate_state_from_headers(state, dict(response.headers))
    if response.status_code == 429:
        force_429_cooldown(state)
    _save_rate_state(state)
```

Call it immediately after **each** FC2MADB response is received, before status, login, JSON, or return handling:

- the initial direct GET;
- a full Inertia fallback GET;
- the deferred actresses GET;
- a 409 retry; and
- the origin response represented by a FlareSolverr solution, when that fallback is used.

The helper must save even when headers are absent if the status is 429. Parse and persist `X-RateLimit-Limit` as well as `X-RateLimit-Remaining` when present. Keep the existing 429 behavior of returning `{}` and forcing a cooldown; apply it uniformly, not only to the first direct GET.

### 4. Do not invoke FlareSolverr before every normal scrape

**Problem**

`_get_flaresolverr_solution(url, cookies)` is called before the direct GET (`fc2madb.py:470`). FlareSolverr's `request.get` fetches the target page, so when it is available this creates an origin request before the direct GET and the deferred actresses GET. A normal scrape therefore uses **three** FC2MADB requests, not the required two, and consumes the whole three-request quota.

**Change**

Start with a direct session and direct initial GET. Invoke FlareSolverr only when that direct request is blocked by Cloudflare (or when a network failure proves the direct request did not reach FC2MADB). After obtaining a solution, rebuild/update the session with the solution's user agent and cookies, record any origin rate headers included in the solution, and use its page body as the initial page.

This preserves the normal two-request flow. Do not make an extra full Inertia request unless the direct/FlareSolverr initial page truly lacks parseable props; that fallback makes a complete scrape three requests and must be treated as a rare, fully rate-accounted path.

### 5. Serialize the rate-state read/wait/request/save sequence

**Problem**

Atomic replacement prevents a corrupt JSON file, but it does not prevent concurrent Stash scraper processes from loading the same state and issuing requests simultaneously. The documented limit is three requests in roughly 1–1.5 seconds, so concurrent Identify jobs can still trip 429.

**Change**

Use an advisory lock file (for example, `rate_state.json.lock` with `fcntl.flock`) around the rate-state load, cooldown wait, origin requests, and saves for one scraper invocation. This intentionally serializes FC2MADB traffic across script processes. Always release the lock in `finally`.

## Documentation/configuration corrections

- `fc2madb.py` defaults `FLARESOLVERR_URL` to `http://localhost:8191/v1`, while `fc2madb/README.md` says the default is `http://9.9.9.200:8191/v1`. For the current Docker topology, make the code default match the documented `9.9.9.200` address, while retaining the environment override.
- Reconcile `RATE_LIMIT_FINDINGS.md` and `SCRAPER_DEVELOPMENT_CACHE.md`: both still describe a current 60-second heuristic in places, but the script currently uses 5 seconds. The observed server window remains approximately 1–1.5 seconds; document the chosen safety margin accurately.

## Acceptance tests for the implementation

Add mock-based tests around `scene_from_url()` (no live cookies required) that demonstrate:

1. unauthenticated direct and fallback 404 responses return `{}`;
2. authenticated, explicit 404 returns only `FC2MADB 404`;
3. authenticated 200 with no valid article returns `{}`;
4. 429, 500, invalid JSON, login-page, missing-version, missing-actresses, and malformed deferred responses return `{}` and never scene fields;
5. a deferred response with `actresses: []` succeeds with zero performers, and one with a name succeeds with that performer;
6. title-only and title-plus-description articles populate only `details`, never `title`;
7. initial, fallback, deferred, and retry responses with rate-limit headers each persist their newest state; every 429 forces and saves cooldown;
8. FlareSolverr is not called during a normal direct success, so the normal scrape makes exactly two origin GETs.
