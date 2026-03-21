# QC Print Agent (On-Prem)

Lightweight Python service that polls `print_jobs` from Supabase and prints ZPL labels to warehouse network printers on TCP/9100.

## Multi-Printer Support

✅ **Supports 2-3 printers per station** - Jobs for different printers print simultaneously
✅ **Shared printer support** - Multiple workstations can share one printer safely (requires hardened backend)

> **Note:** For shared printer scenarios, the Supabase backend must implement the hardened claiming function. See [docs/lovable-requirements.md](docs/lovable-requirements.md) for details.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PRINT_AGENT_CALLBACK_URL` | ✅ | - | Supabase edge function URL |
| `PRINT_AGENT_API_KEY` | ✅ | - | Shared secret for authentication |
| `POLL_INTERVAL_SECONDS` | ❌ | `2` | How often to poll for jobs |
| `PRINTER_PORT` | ❌ | `9100` | TCP port for printer connections |
| `PRINTER_TIMEOUT_SECONDS` | ❌ | `5` | Network timeout in seconds |
| `MAX_CONCURRENT_JOBS` | ❌ | `3` | Number of parallel print workers |
| `LOG_LEVEL` | ❌ | `INFO` | Logging verbosity |
| `LOG_FILE` | ❌ | `print_agent.log` | Log file path (for daemon mode) |
| `WORKSTATION_ID` | ❌ | *auto-generated* | Unique workstation identifier (do not edit manually) |
| `STALE_JOB_TIMEOUT_SECONDS` | ❌ | `300` | Jobs in processing > this are reclaimed |

## Quick Start (Development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment (copy from .env.example)
cp .env.example .env
nano .env  # Edit with your values

# Run in foreground (logs to console)
python3 print_agent.py
```

## Background/Daemon Mode

Run the agent as a background process that survives terminal closure:

```bash
# Start in background mode
python3 print_agent.py --daemon

# Check if agent is running
python3 print_agent.py --status

# Stop the background agent
python3 print_agent.py --stop

# View logs (background mode logs to file)
tail -f print_agent.log  # Linux/macOS
Get-Content print_agent.log -Wait  # PowerShell
```

**Benefits of background mode:**
- ✅ Agent keeps running when you close the terminal
- ✅ Survives session disconnection
- ✅ Logs written to file for later review
- ✅ Graceful shutdown (finishes current jobs before stopping)

## Deployment (Production)

See deployment guide in [docs/deployment.md](docs/deployment.md) or runbook below:

1. **Prepare host** - Linux VM with Python 3.10+, network access to printers and Supabase
2. **Create service user** - `sudo useradd --system qcprint`
3. **Install dependencies** - `pip install -r requirements.txt`
4. **Configure .env** - Set `PRINT_AGENT_CALLBACK_URL` and `PRINT_AGENT_API_KEY`
5. **Install as service** - Create systemd service (see below)
6. **Start and monitor** - `sudo systemctl start qc-print-agent`

### Systemd Service

Create `/etc/systemd/system/qc-print-agent.service`:

```ini
[Unit]
Description=QC Print Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qcprint
WorkingDirectory=/opt/qc-print-agent
EnvironmentFile=/opt/qc-print-agent/.env
ExecStart=/opt/qc-print-agent/.venv/bin/python3 /opt/qc-print-agent/print_agent.py
Restart=always
RestartSec=3

# Security hardening
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
sudo journalctl -u qc-print-agent -f
```

## Multi-Printer Scenarios

| Scenario | `MAX_CONCURRENT_JOBS` | Behavior |
|----------|----------------------|----------|
| Single printer | `1` | Sequential processing |
| 2-3 printers (different IPs) | `3` | Parallel printing to all printers |
| Shared printer (2 workstations) | `1` | Safe - backend enforces exclusivity* |

*\* Requires hardened backend claiming - see [docs/lovable-requirements.md](docs/lovable-requirements.md)*

## Expected Callback Payload

```json
{
  "jobId": "uuid-of-the-print-job",
  "status": "completed",
  "errorMessage": null
}
```

## Expected Database Schema

| Column | Type | Required |
|--------|------|----------|
| `id` | UUID | ✅ |
| `status` | text | ✅ |
| `printer_ip` | text | ✅ |
| `printer_port` | integer | ❌ (default 9100) |
| `zpl_data` | text | ✅ |
| `created_at` | timestamptz | ✅ |
| `picked_up_at` | timestamptz | ❌ (for hardened claiming) |
| `completed_at` | timestamptz | ❌ (for tracking) |
| `agent_id` | UUID | ❌ (for hardened claiming) |
| `error_message` | text | ❌ (for failures) |

## For Lovable Team

If implementing the Supabase backend, see:
- **[docs/lovable-requirements.md](docs/lovable-requirements.md)** - Hardened claiming requirements
- **[docs/supabase/tables/claim_print_jobs.sql](docs/supabase/tables/claim_print_jobs.sql)** - RPC function
- **[docs/supabase/apis/print-agent-edge-function.ts](docs/supabase/apis/print-agent-edge-function.ts)** - Edge function code

## Testing

See [SMOKE_TEST.md](SMOKE_TEST.md) for testing procedures.
