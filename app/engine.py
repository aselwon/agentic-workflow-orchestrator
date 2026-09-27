import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Approval, AuditEvent, Run, now, utc
from app.planner import ToolCall, plan
from app.tools import execute, requires_approval

logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "waiting_human")


def audit(session, run, kind, data):
    sequence = (
        session.scalar(select(func.max(AuditEvent.sequence)).where(AuditEvent.run_id == run.id))
        or 0
    )
    session.add(
        AuditEvent(run_id=run.id, sequence=sequence + 1, step=run.steps + 1, kind=kind, data=data)
    )
    session.flush()


def transition(session, run, status, reason=None):
    previous = run.status
    run.status = status
    run.updated_at = now()
    audit(session, run, "state", {"from": previous, "to": status, "reason": reason})


def fail(session, run, reason):
    run.error = reason
    run.pending = None
    transition(session, run, "failed", reason)


def create_run(session, query, settings):
    run = Run(
        query=query,
        max_steps=settings.max_steps,
        deadline=now() + timedelta(seconds=settings.run_timeout_seconds),
    )
    session.add(run)
    session.flush()
    audit(
        session,
        run,
        "state",
        {
            "from": None,
            "to": "queued",
            "query": query,
            "max_steps": run.max_steps,
            "deadline": utc(run.deadline).isoformat(),
        },
    )
    return run


class DecisionConflict(Exception):
    pass


def decide(session, run_id, approval_id, decision):
    run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if not run:
        raise LookupError("Run not found")
    previous = session.get(Approval, approval_id)
    if previous:
        if previous.run_id == run_id and previous.decision == decision:
            return run
        raise DecisionConflict("This approval already has a different decision")
    if run.status not in ACTIVE or not run.pending or run.pending["id"] != approval_id:
        raise DecisionConflict("No matching pending approval")
    if now() >= utc(run.deadline):
        fail(session, run, "Run deadline exceeded while waiting for approval")
        return run
    if run.status != "waiting_human":
        raise DecisionConflict("Run is not waiting for approval")
    session.add(Approval(id=approval_id, run_id=run_id, decision=decision))
    audit(
        session,
        run,
        "human",
        {"approval_id": approval_id, "decision": decision, "call": run.pending["call"]},
    )
    if decision == "reject":
        fail(session, run, "Rejected by human reviewer")
    else:
        run.pending = {**run.pending, "approved": True}
        transition(session, run, "running", "Human approved the frozen tool arguments")
    return run


class Orchestrator:
    def __init__(self, sessions, settings, transport=None):
        self.sessions = sessions
        self.settings = settings
        self.transport = transport

    def tick(self, run_id):
        with self.sessions.begin() as session:
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update(skip_locked=True)
            )
            if not run or run.status not in ACTIVE:
                return
            if now() >= utc(run.deadline):
                fail(session, run, "Run deadline exceeded (includes human waiting time)")
                return
            if run.status == "waiting_human":
                return
            if run.status == "queued":
                transition(session, run, "running")
                return
            try:
                self.step(session, run)
            except Exception as exc:
                # Log only a bounded class/message; HTTP errors can contain configured service URLs.
                reason = f"Step failed: {type(exc).__name__}"
                if isinstance(exc, (ValueError, RuntimeError)):
                    reason += f": {str(exc)[:300]}"
                audit(session, run, "error", {"message": reason})
                fail(session, run, reason)

    def step(self, session, run):
        pending = run.pending
        call = ToolCall.model_validate(pending["call"]) if pending else plan(run)
        if call is None:
            transition(session, run, "succeeded", "All four workflow tools completed")
            return
        if run.steps >= run.max_steps:
            fail(session, run, "Maximum tool steps exceeded")
            return
        if not pending:
            audit(
                session,
                run,
                "thought",
                {"reason": call.reason, "confidence": call.confidence, "tool": call.tool},
            )
            if requires_approval(call.tool, call.confidence, self.settings.confidence_threshold):
                run.pending = {
                    "id": str(uuid.uuid4()),
                    "call": call.model_dump(),
                    "approved": False,
                    "threshold": self.settings.confidence_threshold,
                }
                audit(session, run, "approval_required", run.pending)
                transition(session, run, "waiting_human")
                return
        elif not pending.get("approved"):
            raise RuntimeError("Pending tool has no approval")
        key = f"step:{run.steps + 1}"
        audit(session, run, "action", {**call.model_dump(), "idempotency_key": key})
        remaining = (utc(run.deadline) - now()).total_seconds()
        if remaining <= 0:
            raise RuntimeError("Run deadline exceeded")
        # Preserve attempted action in the audit, but roll back local effects on failure.
        with session.begin_nested():
            result = execute(
                session,
                run,
                call.tool,
                call.arguments,
                key,
                self.settings,
                timeout=min(self.settings.tool_timeout_seconds, remaining),
                transport=self.transport,
            )
            if now() >= utc(run.deadline):
                raise RuntimeError("Run deadline exceeded during tool call")
        audit(session, run, "observation", {"tool": call.tool, "result": result})
        run.memory = {**run.memory, call.tool: result}
        run.steps += 1
        run.pending = None
        run.updated_at = now()

    def sweep(self):
        with self.sessions() as session:
            ids = list(session.scalars(select(Run.id).where(Run.status.in_(ACTIVE))))
        for run_id in ids:
            self.tick(run_id)

    def worker(self, stop):
        while not stop.is_set():
            try:
                self.sweep()
            except Exception:
                logger.exception("Worker sweep failed; durable runs will be retried")
            stop.wait(self.settings.worker_interval_seconds)
