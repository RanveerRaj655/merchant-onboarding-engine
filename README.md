# Autonomous Merchant Onboarding Platform (AMOP)

48-hour vertical slice: merchants submit an application + documents → stub extraction → async mock PAN/GST(/bank) verification → auto-provision **or** HITL escalation → merchant ID + full audit trail.

**Stack:** Python 3.12 · FastAPI · Celery + Redis · Postgres · Jinja2/HTMX · transactional outbox

> Synthetic KYC fixtures only. Never commit or upload real PAN/GST/bank PII.

## Quick start

```bash
# From repo root
docker compose up --build
```

Services:

| Service | URL |
|---------|-----|
| API + Operator UI | http://localhost:8000 |
| Operator queue | http://localhost:8000/ops |
| Submit fixtures | http://localhost:8000/ops/submit |
| OpenAPI docs | http://localhost:8000/docs |
| Fixture registry | http://localhost:8000/api/v1/fixtures/merchants |
| Mock providers | http://localhost:8001/health |

Operator bearer token (optional): `demo-operator-token`  
Webhook secret: `demo-webhook-secret`

### Fixtures + OCR (important)

We **do not** scrape real PAN/GSTIN or KYC scans from the internet (that is real PII). Instead:

- `fixtures/merchants.json` — synthetic format-valid merchant registry (pass / GST cancelled / PAN inactive / blur / blackout / conflict)
- `fixtures/docs/*.png` — generated PAN card / GST cert / cheque images (clear, blurred, blacked-out)
- **Tesseract OCR** extracts PAN/GSTIN/account from uploaded images (quality gate first)
- Mock verifiers **look up the fixture registry** (ACTIVE vs CANCELLED/INACTIVE)

## 10-minute demo script

1. Open http://localhost:8000/ops/submit — pick a fixture merchant.
2. Happy path (OCR on clear images → registry PASS → APPROVED):

```bash
docker compose exec api python -m scripts.demo_happy
```

3. GST fail (registry `gst_status=CANCELLED` → IN_REVIEW):

```bash
docker compose exec api python -m scripts.demo_gst_fail
```

4. Blurred / blacked-out docs → `MISSING_INFO` + re-upload request:

```bash
docker compose exec api python -m scripts.demo_blurred
docker compose exec api python -m scripts.demo_blacked_out
```

5. Optional callback edge cases:

```bash
docker compose exec api python -m scripts.demo_duplicate_late
```

## Architecture (short)

```
Merchant/API → FastAPI → StateMachine (+ audit + outbox)
                              ↓
                    Outbox relay → Celery workers
                              ↓
              Mock PAN/GST/Bank ──async callback──→ /webhooks/verifications/:provider
                              ↓
                    Aggregate → CHECKS_PASSED → MerchantAccount
                              or IN_REVIEW → Operator UI
```

Only `StateMachineService.transition()` mutates `application.status`.

## Repo layout

```
apps/api/              FastAPI routes, domain services, SQLAlchemy models
apps/worker/           Celery tasks + outbox relay
apps/mock_providers/   Simulated verifiers with delayed HTTP callbacks
apps/operator_web/     Jinja2 + HTMX templates / CSS
packages/shared/       Enums, transition table, Pydantic schemas, masking
scripts/               demo_happy, demo_gst_fail, demo_duplicate_late
alembic/               Migrations
```

## Local tests (no Docker)

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest tests/
```

## Config defaults

See `.env` and Appendix B in the build plan:

- Required verifications: PAN + GST (+ BANK if account present)
- Auto-provision on all checks PASS
- Verification timeout: 30s (demo)
- Extraction confidence min: 0.7

## Definition of done

- [x] `docker compose up` runs API, worker, outbox relay, Postgres, Redis, mock providers, operator web
- [x] Fixture A: happy path → APPROVED + merchant_id
- [x] Fixture B: GST fail → IN_REVIEW; operator can act
- [x] Async callbacks visible in audit / verification panel
- [x] Duplicate callbacks → DUPLICATE (no double apply)
- [x] Late callbacks after timeout → LATE_IGNORED
- [x] Sensitive fields masked by default
- [x] README demo script
