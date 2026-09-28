"""Mornila application content; the provider AgentUpdate envelope is unchanged."""
from typing import Literal

from pydantic import Field, model_validator

from backend.models import Source, StrictModel
from backend.supervision_models import Option, Preferences


class EvidenceSource(Source):
    # What the source establishes. An advertised "from" price is not a fare for the requested dates.
    kind: Literal['verified_for_dates', 'advertised', 'schedule', 'estimate', 'reference', 'unavailable'] | None = None
    note: str | None = Field(default=None, max_length=500)  # agents annotate sources; harmless, kept


class Begin(StrictModel):
    text: str = Field(min_length=5, max_length=12000)
    hours: float = Field(default=8, ge=0.05, le=8, allow_inf_nan=False)
    verification: bool = False


class SetupAnswer(StrictModel):
    step: Literal['dates', 'budget', 'travel', 'stay']
    preference_version: int = Field(ge=1)
    answer: dict


class Revise(StrictModel):
    preference_version: int = Field(ge=1)


class Say(StrictModel):
    text: str = Field(min_length=1, max_length=6000)
    preference_version: int = Field(ge=1)
    decision_id: str | None = Field(default=None, max_length=64)


class Steer(StrictModel):
    decision_id: str = Field(max_length=64)
    action: Literal['approve', 'deny', 'redirect']
    note: str = Field(default='', max_length=2000)
    preference_version: int = Field(ge=1)


class Start(StrictModel):
    brief_event_id: str
    preference_version: int = Field(ge=1)


class Choice(StrictModel):
    issue: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=160)
    proposed_choice: str = Field(min_length=1, max_length=1600)
    action: Literal['research', 'plan']
    option_id: str | None = None
    alternatives: list[str] = Field(default_factory=list, max_length=5)
    implications: str = Field(min_length=1, max_length=1600)
    affected_constraint: str = Field(min_length=1, max_length=300)
    consequence_of_waiting: str = Field(default='No source-backed expiry established.', max_length=800)


class HumanQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    why: str = Field(min_length=1, max_length=800)
    options: list[str] = Field(default_factory=list, max_length=5)
    meanwhile: str = Field(min_length=1, max_length=1600)


class ReviewResult(StrictModel):
    decision_id: str
    outcome: Literal['approve', 'deny', 'redirect', 'defer_to_user']
    explanation: str = Field(min_length=1, max_length=2000)
    uncertainty: list[str] = Field(default_factory=list, max_length=10)
    instruction: str = Field(default='', max_length=3000)
    human: HumanQuestion | None = None

    @model_validator(mode='after')
    def complete(self):
        if self.outcome == 'defer_to_user' and not self.human:
            raise ValueError('Deferral requires a human question and useful permitted work.')
        if self.outcome != 'defer_to_user' and not self.instruction:
            raise ValueError('Resolution requires a concrete research or planning instruction.')
        return self


class Content(StrictModel):
    schema_version: Literal['good-morning.v1']
    category: Literal['onboarding', 'context', 'decision_required', 'review', 'acknowledgement', 'final']
    text: str = Field(min_length=1, max_length=24000)
    facts: list[Option] = Field(default_factory=list, max_length=20)
    sources: list[EvidenceSource] = Field(default_factory=list, max_length=15)
    unknowns: list[str] = Field(default_factory=list, max_length=12)
    decision: Choice | None = None
    review: ReviewResult | None = None
    brief: Preferences | None = None
    ready: bool = False
    # Structured final-report fields; freeform text stays the exact agent wording.
    recommendation: str | None = Field(default=None, max_length=3000)
    open_questions: list[str] = Field(default_factory=list, max_length=8)
    # Judgment calls an agent made on the user's behalf, surfaced so the user can reverse them.
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    stop_reason: Literal['ready', 'no_permitted_work', 'no_progress'] | None = None

    @model_validator(mode='after')
    def valid(self):
        if self.category == 'decision_required' and not self.decision:
            raise ValueError('A decision message requires a choice.')
        if self.category == 'review' and not self.review:
            raise ValueError('Review result missing.')
        if self.decision and self.decision.option_id and self.decision.option_id not in {f.id for f in self.facts}:
            raise ValueError('Proposed option must be included in facts.')
        if self.category == 'onboarding' and self.ready and not self.brief:
            raise ValueError('Ready for confirmation requires a complete proposed brief.')
        return self
