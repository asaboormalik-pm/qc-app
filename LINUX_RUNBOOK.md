# Linux Smoke Test Runbook

Use this on a Russia-side Linux machine or VM during the live validation call.

## 1. Prepare the folder

Put these files in one folder:

- `print_agent.py`
- `.env`
- `.env.example`
- `requirements.txt`

## 2. Confirm Python

```bash
python3 --version
```

## 3. Create a virtual environment and install dependency

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Fill `.env`

Copy `.env.example` to `.env` and paste the real values:

```text
PRINT_AGENT_CALLBACK_URL=...
PRINT_AGENT_API_KEY=...
```

## 5. Load `.env` and run connectivity checks

```bash
set -a
source .env
set +a
```

Check print-agent endpoint:

```bash
curl -I "$PRINT_AGENT_CALLBACK_URL"
```

Check printer TCP reachability:

```bash
nc -vz <PRINTER_IP> 9100
```

## 6. Start the agent

```bash
python3 print_agent.py
```

A healthy idle start looks like:

```text
INFO print-agent started
```

## 7. Live print test

1. Create one test print job from the app/backend.
2. Watch the console for `processing job=...`.
3. Confirm the label prints.
4. Confirm the backend marks the job completed.

Expected success logs:

```text
INFO processing job=<job-id> printer_ip=<printer-ip>
INFO completed job=<job-id>
```

## 8. Controlled failure test

If feasible, use a known bad printer IP for one job or disconnect the printer network temporarily.

Expected result:

- the agent logs an exception
- callback reports `failed`
- backend records the failure

## 9. Stop the agent

Press `Ctrl+C` in the terminal.
