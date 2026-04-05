# Packaged App Behavior

## Expected Startup Behavior

The packaged app should behave as follows:

- On a clean machine, opening the packaged app launches the setup wizard automatically.
- On an already-paired machine, opening the packaged app skips the wizard and continues normal startup.
- `--setup` always reopens the setup wizard.
- `--reset-pairing` clears local pairing state and relaunches the setup wizard.

## Local State and Logs

On Windows, packaged app state is stored in:

- `%APPDATA%\QCConnector\state.json`
- `%APPDATA%\QCConnector\config.json`
- `%APPDATA%\QCConnector\print_agent.log`

This means a developer machine that has already been paired can cause a newly downloaded packaged build to skip first-run setup. That is expected behavior, not automatically a packaging failure.

## Supported Recovery Paths

Use these supported entrypoints instead of manually deleting files:

- `qc-print-agent.exe --setup`
- `qc-print-agent.exe --reset-pairing`

If `--reset-pairing` reports that another instance is already running, stop the existing instance first with `--stop`.
