# Background Mode Quick Reference

## Starting the Agent

### Foreground Mode (Development)
```bash
python3 print_agent.py
```
- Logs to console
- Stops when terminal closes
- Press Ctrl+C to stop

### Background Mode (Production)
```bash
python3 print_agent.py --daemon
```
- Logs to `print_agent.log`
- Keeps running after terminal closes
- Survives session disconnection
- Graceful shutdown (finishes current jobs)

## Managing Background Agent

### Check Status
```bash
python3 print_agent.py --status
```
Output:
```
Print agent is running (PID=12345)
```
or
```
Print agent is not running
```

### Stop Background Agent
```bash
python3 print_agent.py --stop
```
Output:
```
Stopping print agent (PID=12345)...
Print agent stopped
```

### View Logs
```bash
# Linux/macOS - follow log in real-time
tail -f print_agent.log

# Windows PowerShell - follow log in real-time
Get-Content print_agent.log -Wait

# View last 50 lines
tail -n 50 print_agent.log  # Linux/macOS
Get-Content print_agent.log -Tail 50  # Windows
```

## What's Changed

### Signal Handling
Agent now handles shutdown signals gracefully:
- **SIGTERM** (kill command) - Graceful shutdown
- **SIGINT** (Ctrl+C) - Graceful shutdown
- **SIGHUP** (terminal close) - Graceful shutdown (Unix only)

When signal received:
1. Sets `shutdown_requested` flag
2. Finishes current in-progress jobs
3. Waits up to 5 minutes for job completion
4. Exits cleanly

### PID File
- File: `print_agent.pid`
- Created on startup
- Prevents multiple instances
- Removed on clean shutdown
- Used by `--stop` and `--status` commands

### Background Logging
When using `--daemon`:
- Console output redirected to `print_agent.log`
- Append mode (doesn't overwrite old logs)
- Configurable via `LOG_FILE` environment variable
- Standard log format with timestamps

## Examples

### Start and Verify
```bash
# Start in background
$ python3 print_agent.py --daemon
Starting print agent in background mode (PID=12345)
Logs are being written to: print_agent.log
Use --stop to stop the daemon
Use --status to check if it's running

# Verify it's running
$ python3 print_agent.py --status
Print agent is running (PID=12345)
```

### Close Terminal, Agent Keeps Running
```bash
# Start agent
$ python3 print_agent.py --daemon
Starting print agent in background mode (PID=12345)...

# Close terminal or disconnect
$ exit

# Login again and check
$ python3 print_agent.py --status
Print agent is running (PID=12345)  # Still running!
```

### View Logs While Running
```bash
# In one terminal - follow logs
$ tail -f print_agent.log
2026-03-19 10:00:00 INFO print-agent started (max_concurrent_jobs=3)
2026-03-19 10:00:02 INFO Fetched 1 jobs, submitting to thread pool
2026-03-19 10:00:02 INFO [PrinterWorker-0] processing job=abc-123...
2026-03-19 10:00:03 INFO [PrinterWorker-0] completed job=abc-123
```

### Graceful Shutdown
```bash
# Stop agent (finishes current jobs first)
$ python3 print_agent.py --stop
Stopping print agent (PID=12345)...
2026-03-19 10:05:00 INFO Received signal 15, initiating graceful shutdown...
2026-03-19 10:05:01 INFO Finishing 2 in-progress jobs...
2026-03-19 10:05:03 INFO [PrinterWorker-0] completed job=def-456
2026-03-19 10:05:04 INFO [PrinterWorker-1] completed job=ghi-789
2026-03-19 10:05:04 INFO Print agent shutdown complete
Print agent stopped
```

## Troubleshooting

### "Another instance is already running"
```bash
$ python3 print_agent.py --daemon
ERROR: Another instance is already running!
Use --stop to stop the existing instance first

# Solution: Stop the existing instance first
$ python3 print_agent.py --stop
$ python3 print_agent.py --daemon
```

### Agent not stopping with --stop
```bash
# Force kill (last resort)
$ cat print_agent.pid
12345
$ kill -9 12345  # Linux/macOS
$ taskkill /F /PID 12345  # Windows
```

### No log file created
```bash
# Check if LOG_FILE is set
$ echo $LOG_FILE

# Set default if needed
$ export LOG_FILE=print_agent.log
$ python3 print_agent.py --daemon
```

## Integration with Service Managers

### systemd (Linux)
Create `/etc/systemd/system/qc-print-agent.service`:
```ini
[Unit]
Description=QC Print Agent
After=network-online.target

[Service]
Type=simple
User=qcprint
WorkingDirectory=/opt/qc-print-agent
EnvironmentFile=/opt/qc-print-agent/.env
ExecStart=/opt/qc-print-agent/.venv/bin/python print_agent.py
Restart=always

[Install]
WantedBy=multi-user.target
```

### LaunchDaemon (macOS)
Create `/Library/LaunchDaemons/com.qc.print-agent.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.qc.print-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/opt/qc-print-agent/print_agent.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

### Windows Service
Use NSSIS or win32py to create Windows service installer (see Phase 3 of implementation plan).

## Migration from Foreground to Background

### Before (Terminal Mode)
```bash
# Terminal window must stay open
$ python3 print_agent.py
2026-03-19 10:00:00 INFO print-agent started...
[Logs to console]
❌ If terminal closes, agent stops
```

### After (Background Mode)
```bash
# Start in background
$ python3 print_agent.py --daemon
Starting print agent in background mode...
✅ Close terminal anytime
✅ Agent keeps running
✅ Logs to file
```

## Configuration

Add to `.env` file:
```bash
# Background mode log file location
LOG_FILE=print_agent.log

# Or specify full path
LOG_FILE=/var/log/qc-print-agent/agent.log
```

## Security Considerations

- PID file created in current directory (same as `.env`)
- Log files may contain sensitive ZPL data
- Set appropriate file permissions in production
- Consider using `/var/log` for production deployments

## Performance Impact

- **Signal handling**: Negligible overhead (<1% CPU)
- **PID file**: One file read/write per startup/shutdown
- **Background logging**: Async I/O, no performance impact
- **Memory**: No change from foreground mode
