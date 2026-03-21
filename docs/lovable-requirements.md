# Lovable Integration Requirements

> **For the Lovable Team** - This document describes what needs to be implemented in Supabase to support the hardened multi-printer claiming.

## Overview

The Python print agent (in this repo) has been updated to support:
- **Workstation tracking**: Sends `X-Workstation-Id` header with every request
- **Multi-printer support**: Parallel processing for 2-3 printers per station

**Lovable needs to implement** the database function and edge function described below.

---

## 1. Database Migration: `claim_print_jobs()` RPC Function

### Required Schema Changes

Add these columns to `print_jobs` table if they don't exist:

```sql
ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS picked_up_at TIMESTAMPTZ;
ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS workstation_id UUID;
ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS claim_count INT DEFAULT 0;
```

### RPC Function

Create the `claim_print_jobs()` function in Supabase SQL Editor.

**Function signature:**
```sql
claim_print_jobs(
    p_limit INT DEFAULT 3,
    p_workstation_id UUID DEFAULT NULL,
    p_stale_timeout_seconds INT DEFAULT 300
)
RETURNS TABLE (
    id UUID,
    printer_ip TEXT,
    printer_port INT,
    zpl_data TEXT,
    status TEXT,
    created_at TIMESTAMPTZ,
    picked_up_at TIMESTAMPTZ,
    metadata JSONB
)
```

**Key behaviors:**
1. Recovers stale jobs (`processing` > 300 seconds) back to `pending`
2. Claims pending jobs atomically using `FOR UPDATE SKIP LOCKED`
3. Enforces printer exclusivity (one active job per `printer_ip`)
4. Returns oldest job first per printer (FIFO)
5. Tracks which workstation claimed each job

---

## 2. Edge Function Update: `print-agent`

Update the existing `print-agent` edge function to call the RPC instead of direct SELECT.

**New behavior (fixed):**
```typescript
// Extract workstation identity from header
const workstationId = req.headers.get("X-Workstation-Id");

// Call RPC - atomic claiming with printer exclusivity
const { data: jobs, error } = await supabaseAdmin.rpc("claim_print_jobs", {
    p_limit: parseInt(url.searchParams.get("limit") || "3"),
    p_workstation_id: workstationId,
    p_stale_timeout_seconds: 300
});

// Jobs are already marked as 'processing' by the RPC
return new Response(JSON.stringify({ jobs: jobs || [] }));
```

---

## 3. Update Job Status Callback

The callback endpoint should set `completed_at` timestamp:

```typescript
if (status === "completed") {
    updateData.completed_at = new Date().toISOString();
    updateData.error_message = null;
} else if (status === "failed") {
    updateData.completed_at = new Date().toISOString();
    if (errorMessage) {
        updateData.error_message = errorMessage.substring(0, 500);
    }
}
```

---

## 4. CORS Headers Update

Ensure `x-workstation-id` is allowed in CORS:

```typescript
const corsHeaders = {
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type, x-api-key, x-workstation-id",
  //                                           ^^^^^^^^^^^^^^^^^^^ ADD THIS
};
```

---

## Python Agent Request Headers

The agent now sends these headers with every request:

```http
X-API-Key: <configured_api_key>
X-Workstation-Id: <uuid_v4>  # NEW - Unique workstation identifier
Content-Type: application/json
```

---

## Testing Checklist

After Lovable implements these changes, test:

### Test 1: Single Workstation, Multiple Printers
```bash
# Insert 3 jobs for 3 different printers
# Agent polls with limit=3
# Expected: All 3 jobs returned (parallel printing)
```

### Test 2: Single Workstation, Same Printer
```bash
# Insert 3 jobs for SAME printer
# Agent polls with limit=3
# Expected: Only 1 job returned (oldest)
```

### Test 3: Two Workstations, Shared Printer
```bash
# 2 workstations poll simultaneously for same printer
# Expected: Only 1 workstation gets the job, other gets empty
```

### Test 4: Stale Job Recovery
```bash
# Mark job as 'processing' with picked_up_at = 10 minutes ago
# Agent polls
# Expected: Job is recovered and returned as pending
```

---

## Files for Reference

| File | Purpose |
|------|---------|
| [README.md](../../../README.md) - Agent overview |
| [SMOKE_TEST.md](../../../SMOKE_TEST.md) - Expected edge function contract |
| [.env.example](../../../.env.example) - Configuration template |
