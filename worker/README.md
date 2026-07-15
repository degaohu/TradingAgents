# TradingAgents VPS worker

The heavy Python worker daemon. Pulls queued jobs from the Cloudflare
API (Hono on Workers), runs `TradingAgentsGraph.propagate` locally,
streams progress chunks back, writes the final report to R2, and
POSTs completion metadata to the API.

## Why a VPS (and not Workers / Lambda)

- One analysis takes 5–15 minutes wall clock (dozens of LLM calls +
  data-fetch + multi-round debate). Workers cap at 30 s CPU / 15 min
  wall clock and impose per-request memory ceilings; not viable.
- The process is I/O-bound (mostly waiting on LLM providers), so a
  small VPS (Hetzner CX22 or Alibaba light 2C4G) can run many
  concurrent jobs cheaply. Compute is not the bottleneck.
- All secrets (LLM API keys, DB, R2 creds) live in one place under
  systemd env; nothing hits the browser.

## Layout

```
worker/
├── daemon.py         # Main loop: claim → run → finish
├── runner.py         # Wraps TradingAgentsGraph.propagate + PipelineTracker
├── cf_client.py      # Signed HTTP client for the API worker (/internal)
├── r2_writer.py      # boto3 S3-compatible client for R2
├── config.py         # Env-var loader
├── systemd/
│   └── tradingagents-worker.service
├── Dockerfile        # Optional containerized deploy
├── requirements.txt
└── README.md         # (this file)
```

## Prerequisites on the VPS

- Python 3.12+
- `cloudflared` (for the Cloudflare Tunnel back to Workers — optional
  but recommended so the worker never exposes an inbound port; all
  traffic is outbound HTTPS)
- `systemd` (Ubuntu/Debian default)

## First-time setup

```bash
# 1. Clone
git clone <repo> /opt/tradingagents
cd /opt/tradingagents

# 2. Install
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .            # installs tradingagents + web deps
pip install -r worker/requirements.txt

# 3. Config (see .env.example)
cp worker/.env.example /etc/tradingagents-worker.env
sudo chmod 600 /etc/tradingagents-worker.env
sudo vim /etc/tradingagents-worker.env    # fill CF_API_BASE, CF_INTERNAL_TOKEN, LLM keys

# 4. Systemd
sudo cp worker/systemd/tradingagents-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-worker
sudo systemctl status tradingagents-worker
sudo journalctl -u tradingagents-worker -f    # tail logs
```

## Local development (no VPS yet)

```bash
export CF_API_BASE=http://127.0.0.1:8787       # local wrangler dev
export CF_INTERNAL_TOKEN=change-me
export WORKER_ID=dev-laptop
python -m worker.daemon
```

Or run one job end-to-end from CLI without polling:

```bash
python -m worker.daemon --once --ticker AAPL --trade-date 2026-07-14
```
