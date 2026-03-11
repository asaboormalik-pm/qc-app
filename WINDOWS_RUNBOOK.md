# Windows Smoke Test Runbook

Use this on a Russia-side Windows laptop or desktop during the live validation call.

## 1. Prepare the folder

Put these files in one folder:

- `print_agent.py`
- `.env`
- `.env.example`
- `requirements.txt`

## 2. Install Python

Install Python 3.10 or newer from python.org and enable the option to add Python to `PATH`.

Open PowerShell in the project folder and confirm Python:

```powershell
python --version
```

## 3. Install dependency

```powershell
python -m pip install -r requirements.txt
```

## 4. Fill `.env`

Copy `.env.example` to `.env` and paste the real values:

```text
PRINT_AGENT_CALLBACK_URL=...
PRINT_AGENT_API_KEY=...
```

## 5. Connectivity checks

Open PowerShell in the project folder and load the `.env` values into the current session:

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
```

Check print-agent endpoint:

```powershell
Invoke-WebRequest -UseBasicParsing `
  -Uri $env:PRINT_AGENT_CALLBACK_URL `
  -Headers @{ "X-API-Key" = $env:PRINT_AGENT_API_KEY }
```

Check printer TCP reachability:

```powershell
Test-NetConnection -ComputerName <PRINTER_IP> -Port 9100
```

`TcpTestSucceeded : True` means the device can reach the printer.

## 6. Start the agent

```powershell
python .\print_agent.py
```

Leave the PowerShell window open. A healthy idle start looks like:

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

Press `Ctrl+C` in the PowerShell window.
