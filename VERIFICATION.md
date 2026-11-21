# Verification status

Partial verification; not an exhaustive sign-off.

## Evidence
Baseline eight tests passed. Expanded suite exercises all 49 image quality fixtures, 21 actual Tesseract extraction cases, corrupt-image handling, and four workflow regressions. Workflow tests mock database sessions; they do not prove transactional or concurrency behavior.

Four clear GST fixtures (M-003, M-004, M-006, M-007) have actual OCR recognition errors. These are strict expected failures, not passing checks. M-007 had an invalid PAN in the fixture registry; corrected it and regenerated images. Missing the required document identifier now yields an unreadable result rather than accepting an unrelated extracted field.

## Changes
- Ignore stale verification dispatch outside pending verification.
- Scope callback correlation to provider.
- Ignore callbacks after deadline, even before timeout worker execution.
- Prevent terminal request results from being overwritten by a new callback event.
- Avoid invalid audit foreign keys for repeated unknown callbacks.
- Flush callback updates before aggregate evaluation.
- Escalate inconclusive checks.
- Do not let old timeout jobs expire newer attempts prematurely.
- Require passing checks and unchanged identity/bank fingerprint for approval and provisioning.
- Return invalid-image extraction failures; sanitize upload filename basename.
- Minor UI cleanup: flat background, compact typography, smaller corners, focus indicators, reduced-motion support, sandbox context.

## Requirement matrix
| Requirement | Assessment |
|---|---|
| Applications and structured/image documents | Implemented; live API verification outstanding |
| Fields and confidence | Real OCR tested; four known recognition failures |
| Multiple simulated providers | PAN, GST, BANK present; risk provider absent |
| State rules | Explicit transitions with baseline unit coverage |
| Async callbacks | Implemented and patched; live and concurrent tests outstanding |
| Operator status/missing info/review | Routes/templates present; browser verification outstanding |
| Approval and merchant ID | Gate strengthened; integration tests outstanding |
| PDF/multimodal | No PDF rasterization or multimodal model |
| Duplicate/entity matching | Hash-based matching present; concurrency/fuzzy matching unverified |
| Durable workflow | SQL outbox and Celery; crash/replay behavior unverified |
| Human review | Present; failed checks cannot be approved through override |
| Retry/dead-letter | Send exhaustion records exist; other task exhaustion incomplete |
| Metrics | Route present; numerical correctness unverified |

## Remaining high-priority work
1. Execute isolated Postgres/Redis/API/mock-provider/worker demo, covering happy path and GST failure/correction. Docker socket access was denied in this environment.
2. Concurrent callback uniqueness races and consistent application/request locking.
3. Sender completion can overwrite callback terminal status with SENT; conditional database update needed.
4. Bank/identity edits across all states, including approved merchants and queued provisioning.
5. Provisioning failure/recovery and all retry-exhaustion paths.
6. OCR exact-value assertions against registry; rotate, crop, low confidence, contradictory document and oversized upload fixtures.
7. Recursive callback PII redaction and authorization coverage.
8. Relay publish-before-commit replay and extraction task idempotence.
9. Browser interaction/accessibility review and visual comparison.

UI reference consulted: https://www.pinelabs.com/docs/online-payments/dashboard
The changes are a restrained cleanup, not an exact reproduction of the official dashboard.

## HITL refresh and recovery follow-up
Polling now runs only in background-work states and stops for review, missing information, extraction failure, checks-passed awaiting an operator, and terminal states. Removed the load-trigger refresh loop. Forms retain current values instead of silently substituting another merchant's GSTIN. Auto is the default; unreadable evidence requires re-upload, uploaded evidence is extracted even if Verify is chosen, and conflicts cannot bypass extraction. UI correction errors roll back the transaction.

Timeouts: HITL has no expiry. Verification default is 30 seconds; the schema has no maximum bound. The submission timeout demo sets 8 seconds and mock callback delay is 120 seconds. Actual escalation timing depends on worker/relay availability.

Fixture recovery:
- M-001: no correction expected; approved is terminal.
- M-002: cancelled GST cannot be waived. Supply valid matching PAN/GST evidence or reject. Demo-only identity replacement must use a coherent pair; no automatic cross-merchant substitution.
- M-003: inactive PAN requires valid matching identity evidence or rejection. GST OCR may first need a clearer replacement.
- M-004: upload clear PAN/GST documents and use Auto. Current clear GST fixture has a known OCR failure; regeneration/alternate evidence still needed.
- M-005: upload clear PAN/GST replacements and use Auto.
- M-006: align form to document PAN AABCM1111F / GSTIN 09AABCM1111F1Z7 and re-extract; known GST OCR failure requires clearer replacement.
- M-007: resolve GST OCR first, then Retry provision after simulated provisioning failure; current checks must pass.
- Timeout/outage: re-verify unchanged details after provider recovery; correction no longer mandates identity changes.
- Bank failure: correct account and IFSC together, supply matching cheque, use Auto.

These are recovery routes, not claims that all fixtures now pass end-to-end. Four clear GST OCR defects and live integration/browser verification remain outstanding.
