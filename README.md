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

## Step-by-step deployment guide (for Warehouse IT)

Use this section as a runbook for deploying in the warehouse network.

### 1) Prepare host machine

- Use an always-on machine in the same network segment that can reach:
  - Your cloud Supabase/PostgREST endpoint over HTTPS (443)
  - All label printers over TCP 9100
- Recommended: Linux VM/server (Ubuntu 22.04+).
- Ensure Python 3.10+ is installed.

### 2) Create service account and folder

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin qcprint
sudo mkdir -p /opt/qc-print-agent
sudo chown -R qcprint:qcprint /opt/qc-print-agent
```

Copy `print_agent.py` and this `README.md` into `/opt/qc-print-agent`.

### 3) Create Python virtual environment and install dependencies

```bash
cd /opt/qc-print-agent
sudo -u qcprint python3 -m venv .venv
sudo -u qcprint /opt/qc-print-agent/.venv/bin/pip install --upgrade pip
sudo -u qcprint /opt/qc-print-agent/.venv/bin/pip install requests
```

### 4) Add environment configuration

Create `/opt/qc-print-agent/.env`:

```bash
sudo -u qcprint tee /opt/qc-print-agent/.env >/dev/null <<'EOF'
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
POLL_INTERVAL_SECONDS=2
PRINTER_PORT=9100
PRINTER_TIMEOUT_SECONDS=5
LOG_LEVEL=INFO
EOF
```

Security recommendations:
- Restrict file permissions to the service account.
- Never commit `.env` into source control.

```bash
sudo chown qcprint:qcprint /opt/qc-print-agent/.env
sudo chmod 600 /opt/qc-print-agent/.env
```

### 5) Network/firewall validation

From the host, verify:

```bash
# Cloud connectivity
curl -I https://<project>.supabase.co

# Printer reachability (repeat for each printer IP)
nc -vz <printer_ip> 9100
```

If these checks fail, resolve routing/firewall before proceeding.

### 6) Smoke run in foreground (first test)

```bash
cd /opt/qc-print-agent
sudo -u qcprint env -i HOME=/home/qcprint PATH=/usr/bin:/bin \
  /opt/qc-print-agent/.venv/bin/python3 /opt/qc-print-agent/print_agent.py
```

Expected behavior:
- Agent starts and waits.
- When a `pending` job is inserted, it moves to `processing` then `completed`.
- On failure it moves to `failed` with `error` populated.

Stop with `Ctrl+C` after validation.

### 7) Install as systemd service (Linux recommended)

Create `/etc/systemd/system/qc-print-agent.service`:

```ini
[Unit]
Description=QC Print Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qcprint
Group=qcprint
WorkingDirectory=/opt/qc-print-agent
EnvironmentFile=/opt/qc-print-agent/.env
ExecStart=/opt/qc-print-agent/.venv/bin/python3 /opt/qc-print-agent/print_agent.py
Restart=always
RestartSec=3

# Hardening (optional but recommended)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable qc-print-agent
sudo systemctl start qc-print-agent
```

### 8) Monitoring and operations

```bash
# Service status
sudo systemctl status qc-print-agent

# Live logs
sudo journalctl -u qc-print-agent -f

# Restart after config change
sudo systemctl restart qc-print-agent
```

### 9) Upgrade procedure

1. Stop service: `sudo systemctl stop qc-print-agent`
2. Replace `print_agent.py` (and docs if needed)
3. Reinstall/upgrade dependency only if changed
4. Start service: `sudo systemctl start qc-print-agent`
5. Validate logs and one test print job

## Quick run (manual mode)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests

export SUPABASE_URL="https://<project>.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="<service-role-key>"
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
