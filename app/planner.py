from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import utc


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["search_tickets", "get_order", "create_draft_reply", "schedule_followup"]
    arguments: dict
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


def plan(run) -> ToolCall | None:
    """Only structured state controls this planner; ticket prose is never an instruction."""
    memory = run.memory
    if "search_tickets" not in memory:
        return ToolCall(
            tool="search_tickets",
            arguments={"query": run.query},
            confidence=1,
            reason="Find support tickets matching the query",
        )
    tickets = memory["search_tickets"]["tickets"]
    if not tickets:
        raise ValueError("No matching tickets; try 'delayed' or 'T-1001'")
    ticket = tickets[0]
    if "get_order" not in memory:
        return ToolCall(
            tool="get_order",
            arguments={"order_id": ticket["order_id"]},
            confidence=1,
            reason="Read the selected ticket's order via mock HTTP",
        )
    if "create_draft_reply" not in memory:
        order = memory["get_order"]
        text = (
            f"Your order {order['order_id']} is {order['status']}. "
            f"Estimated delivery: {order['eta']}. We will follow up with an update."
        )
        return ToolCall(
            tool="create_draft_reply",
            arguments={"ticket_id": ticket["id"], "text": text},
            confidence=0.6,
            reason="Prepare a draft; low confidence requires review",
        )
    if "schedule_followup" not in memory:
        return ToolCall(
            tool="schedule_followup",
            arguments={
                "ticket_id": ticket["id"],
                "when": (utc(run.created_at) + timedelta(days=1)).isoformat(),
            },
            confidence=1,
            reason="Record a follow-up for the next day",
        )
    return None
