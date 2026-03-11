# QC Print Agent Smoke Test

This connector is meant for manual validation before building an installer.
It talks only to the Supabase edge function, not directly to the database REST API.

## Required inputs

- `PRINT_AGENT_CALLBACK_URL`
- `PRINT_AGENT_API_KEY`

Optional runtime settings:

- `POLL_INTERVAL_SECONDS` default `2`
- `PRINTER_PORT` default `9100`
- `PRINTER_TIMEOUT_SECONDS` default `5`
- `LOG_LEVEL` default `INFO`

## Edge function behavior expected by the agent

- `GET /functions/v1/print-agent?action=poll&limit=1`
- header `X-API-Key: <PRINT_AGENT_API_KEY>`
- response body:

```json
{
  "jobs": [
    {
      "id": "uuid",
      "zpl": "^XA...",
      "printer_ip": "10.0.0.25",
      "status": "processing",
      "created_at": "2026-03-11T10:00:00.000Z"
    }
  ]
}
```

The edge function is responsible for moving fetched jobs from `pending` to `processing`.

## Expected job fields

- `id`
- `status`
- `zpl`
- `printer_ip`
- `created_at`

## Callback payload

```json
{
  "jobId": "uuid-of-the-print-job",
  "status": "completed",
  "errorMessage": null
}
```

## Smoke test sequence

1. Fill `.env` with real values.
2. Verify HTTPS access to the print-agent edge function.
3. Verify TCP access to the printer on port `9100`.
4. Start the agent.
5. Create one test job from the app/backend.
6. Confirm these outcomes:
   - agent logs `processing job=...`
   - printer outputs the label
   - backend receives `completed`
7. Run one controlled failure if feasible and confirm backend receives `failed`.

## Healthy console examples

```text
2026-03-11 10:00:00,000 INFO print-agent started
2026-03-11 10:05:00,000 INFO processing job=123e4567-e89b-12d3-a456-426614174000 printer_ip=10.0.0.25
2026-03-11 10:05:00,500 INFO completed job=123e4567-e89b-12d3-a456-426614174000
```
