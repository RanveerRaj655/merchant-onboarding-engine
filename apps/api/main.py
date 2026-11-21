import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from apps.api.config import get_settings
from apps.api.routes import applications, fixtures, ops, ui, webhooks

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("amop")

app = FastAPI(
    title="Autonomous Merchant Onboarding Platform",
    version="0.1.0",
    description="48h vertical slice — submit → extract → verify → approve/escalate",
)

static_dir = Path(__file__).resolve().parents[1] / "operator_web" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(applications.router)
app.include_router(fixtures.router)
app.include_router(webhooks.router)
app.include_router(ops.router)
app.include_router(ui.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "api"}


@app.on_event("startup")
def startup():
    Path(settings.doc_storage_path).mkdir(parents=True, exist_ok=True)
    logger.info("AMOP API started")
