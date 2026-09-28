from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

Agent = Literal["instinct", "grok"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    url: HttpUrl
    title: str = Field(min_length=1, max_length=300)
    checked_at: AwareDatetime | None = None


class Alternative(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    cost_difference: str = Field(min_length=1, max_length=300)
    time_difference: str = Field(min_length=1, max_length=300)


class RequestedDecision(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    recommendation: str = Field(min_length=1, max_length=2000)
    constraint: str = Field(min_length=1, max_length=600)
    alternatives: list[Alternative] = Field(min_length=1, max_length=5)
    consequence_of_waiting: str = Field(min_length=1, max_length=1000)


class AgentUpdate(StrictModel):
    task_id: UUID
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    update_type: Literal["acknowledgement", "progress", "result", "decision", "error"]
    content: str = Field(min_length=1, max_length=50000)
    sources: list[Source] = Field(default_factory=list, max_length=30)
    requested_decision: RequestedDecision | None = None
    request_id: UUID | None = None
    constraint_version: int | None = Field(default=None, ge=1)
    timestamp: AwareDatetime | None = None
    claims: list[str] = Field(default_factory=list, max_length=10)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)
    disagreements: list[str] = Field(default_factory=list, max_length=10)
    corrections: list[str] = Field(default_factory=list, max_length=10)


class TaskInput(StrictModel):
    agent: Literal["instinct", "grok", "both"]
    instructions: str = Field(min_length=5, max_length=12000)
    travel_year: int | None = Field(default=None, ge=2026, le=2040)
    purpose: Literal["research", "connection_test"] = "research"


class RunInput(StrictModel):
    instructions: str = Field(min_length=5, max_length=12000)
    travel_year: int = Field(ge=2026, le=2040)
    hours: float = Field(default=8, gt=0, le=8)


class AssignInput(StrictModel):
    task_id: UUID


class FollowupTarget(StrictModel):
    task_id: UUID
    proposal_id: str | None


class GroupFollowupInput(StrictModel):
    note: str = Field(min_length=1, max_length=6000)
    targets: list[FollowupTarget] = Field(min_length=1, max_length=2)


class DecisionInput(StrictModel):
    action: Literal["approve", "deny", "redirect"]
    proposal_id: str | None
    note: str = Field(default="", max_length=6000)
    scope: Literal["this_decision"] = "this_decision"


CONSTRAINTS = {
    "route": ["Toronto", "New York", "Boston", "Toronto"],
    "departure": "October 10",
    "return_by": "October 18",
    "currency": "CAD",
    "budget_total": 1500,
    "includes": ["transport", "lodging", "food", "local transit"],
    "window_seat": "where possible",
    "early_departures": True,
    "overnight_buses": True,
    "private_accommodation": "preferred, not mandatory",
    "max_layover_hours": 4,
    "tradeoff": "Two additional travel hours acceptable to save CAD $50; hard constraints still apply.",
    "authority": "Research only. No purchases, reservations, or paid holds.",
}
