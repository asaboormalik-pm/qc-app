# QC Print Agent (On-Prem)

Lightweight Python service that polls `print_jobs` from Supabase and prints ZPL labels to warehouse network printers on TCP/9100.

## URL and auth key setup (important)

Do **not** hardcode these in source code. Configure them during deployment through environment variables (or a local `.env` file loaded by the agent):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

This keeps credentials out of git and lets you promote the same code across dev/staging/prod.

## Environment variables

- `SUPABASE_URL` (required)
- `SUPABASE_SERVICE_ROLE_KEY` (required)
- `POLL_INTERVAL_SECONDS` (optional, default: `2`)
- `PRINTER_PORT` (optional, default: `9100`)
- `PRINTER_TIMEOUT_SECONDS` (optional, default: `5`)
- `LOG_LEVEL` (optional, default: `INFO`)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests
```

## Run

### Option A: shell environment

```bash
export SUPABASE_URL="https://<project>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
python3 print_agent.py
```

### Option B: `.env` file in the same folder as `print_agent.py`

```bash
cat > .env <<'EOF'
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
POLL_INTERVAL_SECONDS=2
EOF

python3 print_agent.py
```

## Expected `print_jobs` fields

The sample agent expects the following fields:

- `id`
- `status` (`pending` -> `processing` -> `completed|failed`)
- `zpl`
- `printer_ip`
- `created_at`
- `processing_started_at` (optional but recommended)
- `error` (optional but recommended)

Adjust the query/payload in `print_agent.py` if your schema differs.
