# FC2MADB Stash scraper

Copy `fc2madb.py`, `fc2madb.yml`, and the supplied `cookies.json` into Stash's `scrapers` directory, then reload scrapers in Stash.

The scraper uses FlareSolverr at `http://9.9.9.200:8191/v1` by default. Override it with `FLARESOLVERR_URL` if the container is reached at another address. `FC2CMADB_COOKIE_FILE` can point to a different browser cookie export; alternatively set `FC2CMADB_SESSION` and `FC2CMADB_XSRF_TOKEN`.

`cookies.json` is local authentication material and is intentionally excluded from the package manifest and `.gitignore`d.
