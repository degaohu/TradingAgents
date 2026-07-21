# TradingAgents — Agent Instructions

## Version & deploy discipline (MANDATORY — every shippable change)

This repo had a real incident: 6 commits piled up locally (through v0.4.1) while
production stayed on v0.4.0, because deploys are manual and nothing enforced
doing one after each change. Don't repeat it. Whenever you change `web/` or
`static/` (or any other user-facing behavior) and consider the work done,
complete this checklist — don't stop partway through:

1. **Bump the version** in `pyproject.toml`'s `[project] version` (semver:
   patch = fix/tweak, minor = new feature, major = breaking change). This is
   the single source of truth — `web/version.py` reads it at runtime and
   serves it via `GET /api/me`'s `version` field for the in-app account bar.
   It also drives the PWA service worker's cache-busting key: `GET /sw.js`
   is served dynamically by `web/routes.py`'s `service_worker()` (not
   straight from `static/sw.js` on disk), which stamps the current version
   into the `CACHE` constant at request time — so bumping this version is
   the *only* step needed to force browsers with an old cached page (e.g.
   a stale `/login`) to pick up the new one. (This used to be a
   hand-maintained constant in `static/sw.js` that silently drifted out of
   sync for releases at a time — don't reintroduce that by hardcoding it
   again.)
2. **Run the test suite** — `.venv/bin/python -m pytest tests/ -q` — and don't
   proceed if anything fails.
3. **Commit** (new commit, not `--amend`, unless the user explicitly says
   otherwise).
4. **Push** — `git push`. This does **not** deploy anything: Railway has no
   GitHub integration configured on this project (confirmed via `railway
   status --json`, `source: None`). Pushing without the next step leaves
   production stale.
5. **Deploy** — `railway up --service tradingagents --detach`, then poll
   `railway status --json` until the new deployment reports
   `SUCCESS`/`RUNNING`.
6. **Verify** — log in to the live site and confirm `GET /api/me`'s `version`
   field matches what you bumped to in step 1. Don't call the work finished
   until this matches.

A `/release` skill in `.claude/skills/release/` runs this checklist — use it,
or work through the steps manually, but don't skip verification.

## Key facts worth knowing before touching this repo

- **Persistent data lives on a mounted Railway Volume**, not the container
  filesystem — `web/history.py`, `web/activity.py`, `web/quota.py`, and
  `web/users.py` all read `TRADINGAGENTS_WEB_DATA_DIR` (set to `/data` in
  production) before falling back to `~/.tradingagents` for local dev. Adding
  a new SQLite-backed module? Follow the same pattern or its data will vanish
  on the next deploy.
- **Users are DB-backed, not env-var-backed**, despite `web/routes.py` still
  defining `_USERS`/`_ADMIN_USERS` — those are only the *seed* for a fresh
  database (`web/users.py`, `ensure_seeded`). Editing env vars after the first
  deploy no longer changes real accounts; use the admin panel's password
  reset instead.
- **Only one report-generating job runs at a time** process-wide
  (`web/jobs.py`'s `JobRegistry`) — not per-user. A running job survives a
  closed browser tab for up to 30 minutes (`_ABANDON_GRACE_SECONDS`) before
  being auto-cancelled.
