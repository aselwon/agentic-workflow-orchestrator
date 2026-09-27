import hashlib
import json
import uuid

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models import Draft, Followup, ToolReceipt

TICKETS = [
    {
        "id": "T-1001",
        "order_id": "ORD-1001",
        "subject": "Delayed delivery",
        "body": "My parcel is late. Please check the delivery status.",
    },
    {
        "id": "T-1002",
        "order_id": "ORD-1002",
        "subject": "Order tracking",
        "body": "Where can I find my order tracking update?",
    },
]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchArgs(Arguments):
    query: str = Field(min_length=1, max_length=500)


class OrderArgs(Arguments):
    order_id: str = Field(pattern=r"^ORD-[0-9]{4}$")


class DraftArgs(Arguments):
    ticket_id: str = Field(pattern=r"^T-[0-9]{4}$")
    text: str = Field(min_length=1, max_length=4000)


class FollowupArgs(Arguments):
    ticket_id: str = Field(pattern=r"^T-[0-9]{4}$")
    when: AwareDatetime


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")
    order_id: str = Field(pattern=r"^ORD-[0-9]{4}$")
    status: str = Field(pattern=r"^(delayed|shipped|delivered)$")
    eta: str = Field(max_length=100)


SCHEMAS = {
    "search_tickets": SearchArgs,
    "get_order": OrderArgs,
    "create_draft_reply": DraftArgs,
    "schedule_followup": FollowupArgs,
}


class ToolError(Exception):
    pass


def requires_approval(tool: str, confidence: float, threshold: float) -> bool:
    # Sending is never currently enabled; retain a fail-closed policy for future send tools.
    return tool.startswith("send") or (tool == "create_draft_reply" and confidence < threshold)


def execute(
    session, run, tool: str, arguments: dict, key: str, settings, timeout: float, transport=None
) -> dict:
    if tool not in SCHEMAS:
        raise ToolError("Tool is not allowlisted")
    args = SCHEMAS[tool].model_validate(arguments)
    normalized = args.model_dump(mode="json")
    fingerprint = hashlib.sha256(
        json.dumps([tool, normalized], sort_keys=True).encode()
    ).hexdigest()
    receipt = session.scalar(
        select(ToolReceipt).where(ToolReceipt.run_id == run.id, ToolReceipt.key == key)
    )
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise ToolError("Idempotency key reused with different arguments")
        return receipt.result

    if tool == "search_tickets":
        query = args.query.lower().strip()
        result = {"tickets": [t for t in TICKETS if query in json.dumps(t).lower()]}
    elif tool == "get_order":
        tickets = run.memory.get("search_tickets", {}).get("tickets", [])
        if args.order_id not in {t["order_id"] for t in tickets}:
            raise ToolError("Order is outside this run's retrieved scope")
        with httpx.Client(timeout=timeout, transport=transport, trust_env=False) as client:
            response = client.get(f"{settings.mock_order_url.rstrip('/')}/orders/{args.order_id}")
            response.raise_for_status()
            order = Order.model_validate(response.json())
            if order.order_id != args.order_id:
                raise ToolError("Order response identity mismatch")
            result = order.model_dump(mode="json")
    else:
        tickets = run.memory.get("search_tickets", {}).get("tickets", [])
        if args.ticket_id not in {t["id"] for t in tickets}:
            raise ToolError("Ticket is outside this run's retrieved scope")
        entity_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run.id}/{key}"))
        if tool == "create_draft_reply":
            session.add(Draft(id=entity_id, run_id=run.id, **normalized))
            result = {
                "draft_id": entity_id,
                "ticket_id": args.ticket_id,
                "text": args.text,
                "sent": False,
            }
        else:
            session.add(
                Followup(id=entity_id, run_id=run.id, ticket_id=args.ticket_id, when=args.when)
            )
            result = {"followup_id": entity_id, **normalized}
    session.add(
        ToolReceipt(run_id=run.id, key=key, fingerprint=fingerprint, tool=tool, result=result)
    )
    session.flush()
    return result
