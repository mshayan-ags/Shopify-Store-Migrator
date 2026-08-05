# Running the app

A self-hosted web UI wrapping every transfer/audit/sources script in this repo. No Shopify Partner account or OAuth needed -- connect stores with custom-app access tokens, same as the CLI. No database: connections live only in your browser, and job history is a handful of JSON files on the server.

## Docker (recommended)

```bash
docker compose up --build
```

Then open http://localhost:5000. Job logs/history persist in `./data` (plain JSON files, no database engine), exports persist in `./Results`, both bind-mounted from the host.

Set `FLASK_SECRET_KEY` in a `.env` file (or your shell) before deploying beyond local use:

```
FLASK_SECRET_KEY=some-long-random-string
```

## Local dev (no Docker)

```bash
pip install -r requirements.txt
python run.py
```

Opens on http://localhost:5000 with the Flask dev server (auto-reload on code changes).

## How it works

- **Connections**: registered once per browser, stored entirely in that browser's `localStorage` (Shopify shop + access token, or Wix/BigCommerce API credentials, or a PostgreSQL/MongoDB connection + mapping YAML path). The server never persists these -- they're sent to it only transiently, as part of a "run" or "test" request, to spawn one job or make one test API call.
- **Dashboard**: pick a source connection, a destination Shopify connection, and a step (or "all") from `transfer_all.py`'s step list, plus advanced options (limits, workers, xlsx export, translation locales) under "Advanced options". Dry-run by default; check "Execute" to actually write to the destination.
- **Jobs**: every run is a background OS process (the exact same CLI script you'd run by hand); watch its live log, kill it, or review history. Job metadata is kept in memory and mirrored to `data/jobs/<id>.json` so history survives a restart (a restarted server can't resume a job it no longer has the OS process handle for, so any job still "running" at restart is marked `unknown`).
- **Audit**: run the read-only verification/audit scripts (with `verify_store_migration`'s resource picker and `verify_product_migration`'s limit/workers/deep-images options) and get a report without touching either store.
- **Sources**: run the Wix/BigCommerce/PostgreSQL/MongoDB connectors to produce canonical JSON in `Results/`, then feed that file into a Transfer script's `--import-from <path>` directly from the CLI.

## Important limitations

- **Single process only.** The app tracks running jobs in memory, so it must run with exactly one worker/process (`gunicorn --workers 1`, already set in the `Dockerfile`). Running multiple workers or replicas will make job status/kill/log-tailing unreliable for jobs not owned by the worker handling your request.
- **Connections live in browser localStorage only** -- clearing your browser's site data for this app removes them, and they don't sync across browsers/devices. Access tokens are stored in plaintext there, same trust model as the CLI's plaintext `.env` file; anyone with access to that browser profile can read them via devtools.
- Gift card migration still requires the explicit confirmation checkbox on the Dashboard (mirrors the CLI's two-flag safety gate) since it creates real redeemable balance on the destination store.
