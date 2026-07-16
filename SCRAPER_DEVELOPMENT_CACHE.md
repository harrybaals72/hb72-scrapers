# Scraper Development Cache

A compact, verified cache of Stash behavior relevant to this repository's custom scrapers. Consult this before searching `~/projects/stash/stashapp`; keep entries actionable and source-backed, not exhaustive.

## Fast search guide

- Script scraper protocol and JSON decoding: `pkg/scraper/script.go`
- URL/fragment dispatch and scene inputs: `pkg/scraper/defined_scraper.go`, `pkg/scraper/cache.go`
- Scraped object schema: `pkg/models/model_scraped_item.go`
- Scraped-tag matching/exclusion: `pkg/scraper/tag.go`, `pkg/scraper/postprocessing.go`
- Automated Identify task and field strategies: `internal/identify/identify.go`, `internal/identify/scene.go`, `internal/manager/task_identify.go`
- Interactive scene scrape/apply flow: `ui/v2.5/src/components/Scenes/SceneDetails/SceneScrapeDialog.tsx` and `SceneEditPanel.tsx`

Use focused searches first, for example:

```sh
rg -n "sceneByFragment|sceneByURL|runScraperScript" pkg/scraper
rg -n "func \(g sceneRelationships\) tags|CreateMissing" internal/identify
rg -n "ScrapedScene|ScrapedTag" pkg/models
```

## Verified behavior

### Script scraper contract

- A `script` scraper receives an operation and JSON input via `py_common.util.scraper_args()`; it must write one JSON result to stdout. Stderr is for logs. See `pkg/scraper/script.go`.
- A scene result may contain only a subset of `ScrapedScene` fields. Tags use `{"tags": [{"name": "…"}]}`; `stored_id` is optional. Schema: `pkg/models/model_scraped_item.go` (`ScrapedScene`, `ScrapedTag`).
- An empty object (`{}`) decodes as an empty scraped scene, not a scraper error. Return `null` only when the scraper should yield no result at all.
- A non-zero exit code causes Stash to treat the scrape as an error. Use exit code 1 only for unrecoverable programming errors (bad input, unsupported operation), not for routine operational failures (site not found, rate-limited, auth expired).

### Tags returned by scrapers

- During post-processing, Stash tries to match returned tag names to existing Stash tags and populates their `stored_id`; configured scraper exclusion patterns can remove tags. See `pkg/scraper/tag.go` and `pkg/scraper/postprocessing.go`.
- In the interactive scene scrape dialog, returned tags alone are sufficient to show the dialog. An unmatched tag can be created or linked before applying. See `SceneScrapeDialog.tsx` and `scrapedTags.tsx`.
- In the automated Identify task, a tag with `stored_id` is applied. An unmatched tag is created only when the source's `tags` field option enables `createMissing`; otherwise it is ignored. Tag updates use the configured merge/overwrite strategy. See `internal/identify/scene.go`.

### Failure-mode disambiguation (general pattern)

When a scraper cannot return a complete scene, distinguish **terminal** from **transient** failures:

**Terminal** (e.g. 404 — URL will never resolve): return `{"tags": [{"name": "FC2MADB 404"}]}` with the sentinel tag and **no** `code`, `details`, or `urls` metadata. The tag persists to indicate the URL is permanently unresolvable.

**Transient** (e.g. 429, auth expiry, cloudflare, network error — may succeed on retry): return `{}` (empty dict). No metadata or tags should persist from a failed scrape — the user can retry and a subsequent successful scrape will populate the scene cleanly.

For all failures, log the reason with structured format (e.g. `FAILURE TYPE=<type>  URL=<url>`) for monitoring. The Identify task sees nothing for transient failures (no result to apply), so retries are not polluted by stale sentinel tags.

For the Identify task, pre-create the sentinel tag for terminal-only failures so post-processing resolves it, or enable `createMissing` for tags in the source config.

### Auth detection via Inertia page data

The site embeds `auth.user` in the initial HTML page data (inside `<script data-page="app">`). When cookies are expired or invalid, `auth.user` is `null`. The scraper checks this after extracting the Inertia props — if `auth.user` is not a dict, the user is not logged in and the scraper returns `{}` with an error telling them to re-export cookies.

This check happens BEFORE the Inertia JSON GET for actresses, so no fallback occurs for an unauthenticated session. The auth check covers both the initial-HTML path (props from HTML) and the Inertia JSON GET fallback (full page data from the fallback request).

### Performer/actress retrieval (2 requests per scrape)

The initial HTML page data includes `article` but **not** `actresses` — `actresses` is a **deferred Inertia prop** (`deferredProps: {'default': ['actresses']}`). To retrieve actresses, the scraper makes a second GET with Inertia partial-data headers (`X-Inertia-Partial-Component: Articles/Show`, `X-Inertia-Partial-Data: article,actresses`). This is done AFTER auth and article checks pass.

Confirmed by testing articles 4604611 (has performer 早瀬未来) and 4940229 (no performer):
- 4604611: Inertia GET returns 1 actress (早瀬未来)
- 4940229: Inertia GET returns 0 actresses

The version is extracted fresh from the initial HTML, so 409 (version mismatch) never occurs in practice.

### Rate-limit state: saved on EVERY server-reaching request

Every request that reaches the fc2cmadb server counts against the rate limit. The scraper saves rate state in ALL paths before returning, including:
- Login page detection (direct GET and initial HTML)
- Auth check failure (auth.user is null)
- Article missing (not found)
- 404 (terminal)
- 429 (forced cooldown even without rate-limit headers)
- Inertia fallback failures (login, non-200, parse error)
- Inertia partial-data GET (headers captured before status-code checks)
- Successful completion

The only paths that skip rate-state saving are those where no origin request occurred: unsupported URL, missing cookies, or a direct network error with no FlareSolverr solution. Direct Cloudflare responses and FlareSolverr-represented origin responses are recorded.

A normal scrape uses 2 origin requests (initial HTML GET + Inertia JSON GET). Empty-shell fallback, 409 retry, or Cloudflare recovery can require additional requests and each response is recorded immediately.

### fc2cmadb.com rate-limit behavior (verified 2026-07-16)

See `fc2madb/RATE_LIMIT_FINDINGS.md` for full details.

- Laravel throttle: **limit=3, window=~1-1.5s**. No `X-RateLimit-Reset` or `Retry-After` headers ever.
- 429 response has NO rate-limit headers. Inertia component is `"Error"`, version is `""`.
- `remember_web` cookie is deleted on 429 (Max-Age=0). Session cookies are still refreshed.
- The scraper uses a 30-second cooldown when remaining is 0, and a conservative 5-second heuristic for remaining 1 when no reset header exists.
- Every direct, fallback, deferred, retry, and FlareSolverr-represented origin response is recorded immediately; 429 forces remaining to 0 and persists the 30-second cooldown.
- An advisory lock serializes the rate-state/request sequence across scraper processes.

### fc2cmadb.com 404 behavior (verified 2026-07-17)

Source: `fc2madb/fc2madb.py` test request to `/articles/667478`.

- A non-existent article returns **HTTP 404** with Inertia component `"Error"` (not `"Articles/Show"`).
- The 404 HTML **does** contain Inertia page data with `props.status` (404) and `props.message` (e.g. "No query results for model [App\Models\Article]").
- No `article` key exists in props — the scraper must detect the error component or check for `article` presence.
- Rate-limit headers **are** sent (X-RateLimit-Remaining decrements). The 404 counts against the limit and the scraper **must** save rate-limit state before returning.
- The Inertia JSON GET fallback also returns 404 with component `"Error"` and the same `props.status`/`props.message` in the JSON body.
- 404 is terminal — return **only** the sentinel tag `"FC2MADB 404"` with no `code`, `details`, or `urls` metadata.

## Maintenance rules

- Add or revise an entry only after verifying it in the current Stash source; include the most direct source paths.
- Prefer concise general behavior and search pointers over one-off investigation transcripts. Remove superseded or duplicate entries.
- Never record cookies, tokens, URLs containing credentials, or local scene data.
