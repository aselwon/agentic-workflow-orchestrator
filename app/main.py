import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text

from app.config import Settings
from app.db import Base, make_database
from app.engine import DecisionConflict, Orchestrator, create_run, decide
from app.models import AuditEvent, Run, utc
from app.tools import SCHEMAS

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")


class StartRun(BaseModel):
    query: str = Field(default="delayed", min_length=1, max_length=500)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Query cannot be blank")
        return value.strip()


class Decision(BaseModel):
    approval_id: str = Field(min_length=36, max_length=36)
    decision: str = Field(pattern="^(approve|reject)$")


def snapshot(session, run):
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run.id).order_by(AuditEvent.sequence)
    ).all()
    return {
        "id": run.id,
        "query": run.query,
        "status": run.status,
        "steps": run.steps,
        "max_steps": run.max_steps,
        "memory": run.memory,
        "pending": run.pending,
        "error": run.error,
        "created_at": utc(run.created_at).isoformat(),
        "deadline": utc(run.deadline).isoformat(),
        "events": [
            {
                "sequence": e.sequence,
                "step": e.step,
                "kind": e.kind,
                "data": e.data,
                "created_at": utc(e.created_at).isoformat(),
            }
            for e in events
        ],
    }


def create_app(settings=None, transport=None):
    settings = settings or Settings()
    if not settings.mock_llm:
        raise RuntimeError("MVP supports MOCK_LLM=1 only; no provider calls are configured")
    engine, sessions = make_database(settings.database_url)
    orchestrator = Orchestrator(sessions, settings, transport)

    @asynccontextmanager
    async def lifespan(app):
        Base.metadata.create_all(engine)
        stop = threading.Event()
        thread = threading.Thread(target=orchestrator.worker, args=(stop,), daemon=True)
        if settings.worker_enabled:
            thread.start()
        yield
        stop.set()
        if thread.is_alive():
            thread.join(timeout=settings.tool_timeout_seconds + 2)
        engine.dispose()

    app = FastAPI(title="OpsAgent", version="0.1.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.orchestrator = orchestrator
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse(
                    {"detail": "Cross-origin writes are forbidden"}, status_code=403
                )
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site writes are forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    def read_run(run_id):
        with sessions() as session:
            run = session.get(Run, run_id)
            if not run:
                raise HTTPException(404, "Run not found")
            return snapshot(session, run)

    def apply_decision(run_id, body):
        with sessions.begin() as session:
            try:
                run = decide(session, run_id, body.approval_id, body.decision)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except DecisionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            result = snapshot(session, run)
        return result

    @app.get("/health")
    def health():
        with sessions() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok", "mock_llm": settings.mock_llm}

    @app.get("/api/tools")
    def tools():
        return {name: schema.model_json_schema() for name, schema in SCHEMAS.items()}

    @app.post("/api/runs", status_code=201)
    def start(body: StartRun):
        with sessions.begin() as session:
            run = create_run(session, body.query, settings)
            return snapshot(session, run)

    @app.get("/api/runs")
    def runs():
        with sessions() as session:
            return [
                {"id": r.id, "query": r.query, "status": r.status, "steps": r.steps}
                for r in session.scalars(select(Run).order_by(Run.created_at.desc()).limit(100))
            ]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return read_run(run_id)

    @app.post("/api/runs/{run_id}/decision")
    def decision(run_id: str, body: Decision):
        return apply_decision(run_id, body)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request=request, name="index.html", context={"runs": runs()}
        )

    @app.post("/ui/runs")
    def start_ui(query: str = Form(min_length=1, max_length=500)):
        if not query.strip():
            raise HTTPException(422, "Query cannot be blank")
        result = start(StartRun(query=query))
        return HTMLResponse("", headers={"HX-Redirect": f"/runs/{result['id']}"})

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        return templates.TemplateResponse(
            request=request, name="run.html", context={"run": read_run(run_id)}
        )

    @app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    def run_fragment(request: Request, run_id: str):
        return templates.TemplateResponse(
            request=request, name="run_panel.html", context={"run": read_run(run_id)}
        )

    @app.post("/ui/runs/{run_id}/decision", response_class=HTMLResponse)
    def ui_decision(
        request: Request,
        run_id: str,
        approval_id: str = Form(min_length=36, max_length=36),
        decision: str = Form(pattern="^(approve|reject)$"),
    ):
        run = apply_decision(run_id, Decision(approval_id=approval_id, decision=decision))
        return templates.TemplateResponse(
            request=request, name="run_panel.html", context={"run": run}
        )

    return app


app = create_app()
