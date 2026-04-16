# UAT Gap Analysis and Implementation Plan (Warehouse Labelling Platform)

Date: 2026-04-16

## Scope and baseline used

- **Important limitation:** The referenced UAT document (`202604_Labelling. Final.docx.pdf`) is not present in this repository snapshot, so this analysis is based on the feature list provided in the task prompt plus code inspection of the current repo.
- This repository contains a Python **print/ERP connector agent** and tests, not a full warehouse UI/backend product.

## A. Executive Summary

The current codebase is a transport connector focused on:

1. polling print jobs,
2. sending ZPL to printers,
3. posting callback statuses,
4. forwarding two ERP business flows (`erp_box_fetch`, `completion_event`).

Given the 20-feature UAT scope, **most end-user warehouse workflow features are missing from this repo** because there is no frontend app, workflow engine, station/session UI, box management UI, or inventory domain backend present. Only portions of printing transport and ERP box fetch/completion transport are implemented.

Overall status across 20 requested features:

- Implemented: **0** (full UAT-level implementations)
- Partially Implemented: **4** (transport-level subsets)
- Missing: **15**
- Unclear / Needs Specification: **1** (where prompt implies UI behavior but repo has no UI context)

## B. Feature-by-feature Gap Analysis Table

| Feature ID | Feature Name | UAT Requirement Summary | Status | Frontend Evidence | Backend/DB Evidence | Gap Description | Recommended Fix | Estimated Complexity | Regression Risk |
|---|---|---|---|---|---|---|---|---|---|
| 1 | PDF instructions | Fetch via API and show on labelling screen | Missing | No frontend files/routes in repo | No PDF fetch/display API in agent | No UI/API layer for this | Implement in product app: API contract + labelling UI PDF pane | Medium | Low (isolated) |
| 2 | Invoice box info ingestion/use | Receive invoice boxes, split products, close logic, visualize boxes | Partial | No UI | ERP `erp_box_fetch` request/response transport exists | Only fetch+normalize box list; no invoice workflow/state logic | Add domain services + DB schema + UI flows for invoice/box lifecycle | High | High |
| 3 | Priority behavior | Priority badges/filtering and warning behavior | Missing | No invoice management UI | No priority logic in agent | Entirely absent | Add priority fields, filtering rules, scan-time validation | Medium | Medium |
| 4 | Employee entry/station control | Lock checks, inactivity reasons, cross-station blocking | Missing | No auth/session UI | Only workstation header in connector | No station/session management domain | Build session service, station lock model, inactivity flow | High | High |
| 5 | Screen/workflow guidance | Arrows/prompts, fixed layout, colors, translations, warning persistence | Missing | No UI | No translation service in repo | UX layer absent | Implement design system + i18n + workflow prompt engine | High | Medium |
| 6 | Box management improvements | Totals, sequence registration, barcode options, close behavior, anomaly reroute | Missing | No box UI | No domain model for packaging/defect/anomaly operations | Entire box operations domain missing | Add box task engine + APIs + UX wizard flows | High | High |
| 7 | Serial labelling process | Serial mode detection, bulk print, aggregation, first-item checks | Missing | No serial labelling UI | No serial flow state machine | Missing core serial workflow | Implement serial mode orchestration in app/backend | High | High |
| 8 | Unknown product/manual category flow | Not-found barcode path, category/manual params, anomaly placement | Missing | No scan UX flow | No unknown-item classification API | Missing | Add not-found pipeline + parameter forms + anomaly routing | High | High |
| 9 | Individual marking/item rules | Attribute/image/template load, surplus/defect flow, first-unit checks | Missing | No item processing UI | No SKU attribute rule engine in this repo | Missing | Build item-rule engine + UI states + permission checks | High | High |
| 10 | Hands-free scanning special codes | Scan printer barcode to print/attach; scan to damage/anomaly | Partial | No scanner UI | Printer endpoint selection exists by `printer_ip` and callback | No scan-code parser; no damage/anomaly scan actions | Add scanner code map and action dispatcher in app | Medium | Medium |
| 11 | Printing and reprinting | Auto print, barcode print, reprint with supervisor auth, DM link behavior | Partial | No reprint UI | Auto polling/printing + callback exists | Reprint authorization and DM semantics absent | Add reprint workflow, supervisor validation, traceability model | Medium | Medium |
| 12 | Additional services | Show/select services, PHOTO trigger, save/visibility/API transfer | Missing | No services UI | No additional services payload model | Missing | Add service catalog + execution hooks + persistence + API mapping | High | Medium |
| 13 | Description/anomaly workflow | Dedicated anomaly description flow + permissions + statuses | Missing | No description UI | No description role/variation rules | Missing | Build anomaly description module + ACL + status tracking | High | High |
| 14 | Printer setup/paper correctness test | Setup wizard, template preview, paper scan, test print, control scan | Missing | No setup wizard UI | Agent sends raw ZPL only | No paper/template verification pipeline | Add printer setup workflow in product app + validation APIs | Medium | Medium |
| 15 | Performance monitoring | Staff KPI frame + monitoring | Missing | No dashboard UI | No performance metrics aggregation | Missing | Add telemetry model + KPI UI + reporting jobs | Medium | Low |
| 16 | Invoice mgmt visibility/reporting | Lists of items/anomalies/damage with box/DM/UL | Missing | No invoice management UI | No reporting schema/query layer in repo | Missing | Build invoice analytics/reporting endpoints + UI | High | Medium |
| 17 | Relabelling | Separate task, field correction/SKU replacement, old DM extinguish | Missing | No relabelling UI | No relabel domain logic | Missing | Add relabel task type + audit + DM invalidation flow | High | High |
| 18 | Moving between boxes | Movement tasks/statuses/errors/log/tab | Missing | No movement UI | No movement task backend | Missing | Add movement domain model + validations + logging + UI | High | High |
| 19 | Pallet formation/station menu | Pallet scan + box binding + completion payload + error cases | Missing | No pallet UI | No pallet aggregation model in repo | Missing | Build palletization workflow + integrity validations + API payload | High | High |
| 20 | UAT “check/not implemented/spec needed” items | Explicitly verify all check flags from UAT doc | Unclear / Needs Specification | UAT doc unavailable in repo | UAT doc unavailable in repo | Cannot verify explicit checklist notes without source document | Re-run analysis once PDF is provided to repository/workspace | Low | Low |

## C. Code Evidence by Feature

### Implemented/partial capabilities found in code

1. **Print transport loop exists** (poll → process → callback), including parallel workers and callback retry.
2. **ERP agent loop exists** with routing by `business_type`.
3. **ERP box fetch transport exists**, including normalization of returned boxes.
4. **Completion event pass-through exists** with explicit no-mutation intent.
5. **Workstation identity header support exists**.

### Critical evidence pointers

- Core connector purpose (print polling and callback): `print_agent.py` module docstring and README. 
- Print job polling endpoint contract and job fields: `SMOKE_TEST.md`.
- ERP business routing limited to `erp_box_fetch` and `completion_event` only.
- `erp_box_fetch` performs fixed warehouse override and only returns normalized `box_id/sscc/dm_code` list.
- `completion_event` payload is forwarded unchanged (transport style), not warehouse business-rule processed.
- No frontend/routes/components/hooks/supabase migrations/edge functions are present in this repository.

## D. Missing / Partial Implementation Details

### Partially implemented (transport subset only)

- **Feature 2 (Invoice box information ingestion/use):**
  - Implemented subset: fetch + normalize box data from ERP (`box_id`, `sscc`, `dm_code`).
  - Missing: invoice splitting, inbound close control, UI visualization/selection, close-state enforcement.

- **Feature 10 (Hands-free scan codes):**
  - Implemented subset: printer-specific print dispatch by job payload (`printer_ip`, `printer_port`).
  - Missing: scanner barcode command mapping and scan-triggered business actions (damage/anomaly/attach).

- **Feature 11 (Printing/reprinting):**
  - Implemented subset: automatic print polling, send-to-printer, success/failure callback.
  - Missing: supervisor-confirmed reprint, DM linkage semantics, post-print UAT verification steps.

- **Feature 14 (Printer setup/paper correctness):**
  - Implemented subset: low-level ability to print test ZPL if upstream sends it.
  - Missing: setup wizard, template preview, paper type scan/selection, control code scan loop.

### Completely missing in this repo

All workflow-heavy items (priority, station control, anomaly description, relabelling, movement tasks, palletization, performance UI/reporting, translation UX, etc.) require application layers not present in this connector-only repository.

## E. Recommended Implementation Plan

### 1) High business impact + low effort

1. **Formalize and version transport contracts** (`erp_box_fetch`, `completion_event`, print callback) with JSON schemas and strict validation tests.
2. **Extend callback payload tracing fields** (consistent `message_id`, `invoice_id`, station/operator IDs) to improve auditability.
3. **Add explicit reprint intent flag + actor metadata in print jobs** (even before full UI exists) to support later supervisor controls.

### 2) High business impact + medium effort

1. **Build minimal Warehouse Workflow API service** (outside this repo or adjacent service):
   - invoice read model,
   - box lifecycle endpoints,
   - priority filtering checks,
   - anomaly/damage event endpoints.
2. **Implement scanner command interpreter** in app layer:
   - printer attach/change,
   - damage/anomaly declarations,
   - command audit logs.
3. **Implement printer setup flow in app**:
   - template preview endpoint,
   - paper-type validation state,
   - test print + control code confirmation.

### 3) Large/spec-heavy items needing customer clarification

1. **Serial labelling state machine** (feature 7) – requires precise first-unit vs subsequent-unit rule matrix.
2. **Description/anomaly ruleset** (feature 13) – requires authoritative variation and permission model.
3. **Movement and palletization task domains** (features 18–19) – requires data model, status transitions, and API payload contracts.
4. **Relabelling legal/audit behavior** (feature 17) – requires irreversible DM extinguish business/legal rules.

## F. Regression Risks

1. **Transport mutation risk:** completion payloads may accidentally be transformed in future refactors.
2. **Retry/idempotency risk:** callbacks and ERP forwarding retries can duplicate side effects if upstream does not enforce idempotency.
3. **Printer concurrency risk:** shared-printer safety is backend-dependent; connector alone cannot enforce all exclusivity rules.
4. **Observability risk:** without structured domain logs in upstream services, diagnosing UAT workflow failures will be difficult.

## G. Validation / Test Plan

1. **Connector-level regression tests (current repo):**
   - keep and extend existing tests for ERP pass-through and box-fetch normalization,
   - add schema contract tests for callback payloads,
   - add negative tests for malformed job/request payloads.
2. **Integration tests (required in full platform):**
   - invoice with box data end-to-end,
   - priority-only invoice visibility and scan warning,
   - station lock and inactivity return reason flow,
   - anomaly description and reprint authorization flow,
   - pallet and move-task error scenarios.
3. **UAT replay harness:**
   - convert each explicit UAT comment into executable test cases with expected result snapshots.

## H. Open Questions / Spec Ambiguities

1. Please provide the exact `202604_Labelling. Final.docx.pdf` file in repository/workspace so all explicit “check/not implemented/requires spec” rows can be validated directly.
2. Confirm source-of-truth architecture split:
   - Which service owns invoice/box business state?
   - Is this repository intended to remain transport-only?
3. Clarify whether `erp_box_fetch` warehouse override (`warehouse_id="000093"`) is temporary or permanent.
4. For reprint controls, define minimum required supervisor auth mechanism (barcode scan, SSO role, or both).

## What appears already implemented but should be retested because UAT suggests it may still be broken

1. **Parallel printer processing / shared-printer behavior** (depends on backend claim logic).
2. **ERP completion payload pass-through without mutation.**
3. **ERP box-fetch response normalization and callback success/failure handling.**
4. **Callback retry behavior under transient network failures.**
