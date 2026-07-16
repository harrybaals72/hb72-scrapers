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

When a scraper cannot return a complete scene, it can return a scene containing only a sentinel tag and optionally the `code` and `details` fields. This makes the failure reason visible in the scrape dialog and Identify task instead of a silent empty result. Suggested pattern:

- Return the scene's identifier (`code`) so it is not wiped by the scrape.
- Use a distinctive tag name per failure mode (e.g. "Not Found", "Rate Limited", "Auth Error").
- Include the HTTP status or exception message in `details` when available.
- Log the failure reason with structured format (e.g. `FAILURE TYPE=<type>  URL=<url>`) for easy monitoring.
- Distinguish terminal failures (e.g. 404 — scene will never match) from retryable ones (e.g. 429, auth expiry).

For the Identify task, pre-create the sentinel tag so post-processing resolves it, or enable `createMissing` for tags in the source config.

### fc2cmadb.com rate-limit behavior (verified 2026-07-16)

See `fc2madb/RATE_LIMIT_FINDINGS.md` for full details.

- Laravel throttle: **limit=3, window=~1-1.5s**. No `X-RateLimit-Reset` or `Retry-After` headers ever.
- 429 response has NO rate-limit headers. Inertia component is `"Error"`, version is `""`.
- `remember_web` cookie is deleted on 429 (Max-Age=0). Session cookies are still refreshed.
- The scraper's 60-second heuristic cooldown is ~40-60x too long for the actual window.
- 429 is not detected in the primary direct-GET path; it falls through to "everything else" and triggers an unnecessary Inertia fallback GET.

## Maintenance rules

- Add or revise an entry only after verifying it in the current Stash source; include the most direct source paths.
- Prefer concise general behavior and search pointers over one-off investigation transcripts. Remove superseded or duplicate entries.
- Never record cookies, tokens, URLs containing credentials, or local scene data.
