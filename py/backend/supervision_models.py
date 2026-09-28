"""Application request and findings contracts; the existing transport envelope stays intact."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from backend.models import Agent, Source, StrictModel


class Preferences(StrictModel):
    travel_year: int | None = Field(default=None, ge=2026, le=2040)
    start_date: date | None = None
    end_date: date | None = None
    date_flexibility: Literal['fixed', 'flexible', 'unresolved'] = 'unresolved'
    departure_flexibility: Literal['fixed', 'flexible', 'unresolved'] = 'unresolved'
    return_flexibility: Literal['fixed', 'flexible', 'unresolved'] = 'unresolved'
    budget_strictness: Literal['strict', 'target'] = 'strict'
    budget_scope: Literal['whole_trip', 'transport_and_stays', 'transport'] = 'whole_trip'
    travelers: int = Field(default=1, ge=1, le=9)
    budget_target: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    earliest_departure: str | None = Field(default=None, pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    max_travel_hours: float | None = Field(default=None, gt=0, le=48, allow_inf_nan=False)
    location_preference: Literal['central', 'near_transit', 'flexible'] = 'near_transit'
    city_nights: dict[str, int] = Field(default_factory=dict, max_length=8)
    route: list[str] = Field(default=["Toronto", "New York", "Boston", "Toronto"], min_length=2, max_length=8)
    currency: str = Field(default="CAD", pattern=r"^[A-Z]{3}$")
    budget_total: float = Field(default=1500, gt=0, le=1000000, allow_inf_nan=False)
    max_layover_hours: float = Field(default=4, ge=0, le=48, allow_inf_nan=False)
    window_seat: bool = True
    early_departures: bool = True
    overnight_buses: bool = True
    private_accommodation: bool = True
    extra_hours_to_save: float = Field(default=2, ge=0, le=24, allow_inf_nan=False)
    savings_threshold: float = Field(default=50, gt=0, le=100000, allow_inf_nan=False)
    notes: str = Field(default="", max_length=3000)

    @model_validator(mode="after")
    def dates_agree(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Return date must be on or after departure.")
        if self.start_date and self.start_date.year != self.travel_year:
            raise ValueError("Departure date and explicit travel year must agree.")
        if self.end_date and self.end_date.year != self.travel_year:
            raise ValueError("Return date and explicit travel year must agree.")
        if any(not city.strip() or len(city) > 100 or nights < 0 or nights > 60
               for city, nights in self.city_nights.items()):
            raise ValueError('City nights must use named cities and counts from zero to sixty.')
        return self


class Permissions(StrictModel):
    mode: Literal["research_only"] = "research_only"
    allow_format_repair: bool = True


class CreateRequest(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=5, max_length=12000)
    participants: list[Agent] = Field(min_length=1, max_length=2)
    preferences: Preferences
    permissions: Permissions = Field(default_factory=Permissions)
    purpose: Literal["research", "connection_test"] = "research"
    review_together: bool = False
    review_rounds: int = Field(default=1, ge=1, le=2)
    hours: float = Field(default=8, ge=0.05, le=8, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_request(self):
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("Each agent can participate once.")
        if self.review_together and set(self.participants) != {"instinct", "grok"}:
            raise ValueError("Review together requires both participating agents.")
        if self.purpose == "research" and not all(
            [self.preferences.travel_year, self.preferences.start_date, self.preferences.end_date]
        ):
            raise ValueError("Confirm the year, departure and return dates before travel research.")
        return self


class RequestAction(StrictModel):
    kind: Literal["approve", "deny", "redirect", "instruction", "summarize"]
    expected_revision: int = Field(ge=1)
    preference_version: int = Field(ge=1)
    recipients: list[Agent] = Field(min_length=1, max_length=2)
    note: str = Field(default="", max_length=6000)
    decision_id: str | None = None
    scope: Literal["this_request"] = "this_request"


class PreferenceEdit(StrictModel):
    expected_revision: int = Field(ge=1)
    preference_version: int = Field(ge=1)
    preferences: Preferences
    permissions: Permissions
    save_as_default: bool = False


class ReviewSetting(StrictModel):
    expected_revision: int = Field(ge=1)
    enabled: bool
    rounds: int = Field(default=1, ge=1, le=2)


class Cost(StrictModel):
    category: Literal["transport", "accommodation", "food", "local_transit"]
    amount: float | None = Field(default=None, ge=0, le=1000000, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    taxes_included: bool | None = None


class Option(StrictModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1600)
    costs: list[Cost] = Field(default_factory=list, max_length=4)
    layover_hours: float | None = Field(default=None, ge=0, le=240, allow_inf_nan=False)
    travel_hours: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)
    start_date: date | None = None
    end_date: date | None = None
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    checked_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique_categories(self):
        if len({c.category for c in self.costs}) != len(self.costs):
            raise ValueError("Use one total per cost category; missing costs stay unknown.")
        return self


class FindingDecision(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    explanation: str = Field(min_length=1, max_length=1600)
    kind: Literal["constraint_exception", "clarification", "selection"]
    option_id: str | None = None
    consequence_of_waiting: str = Field(
        default="Expiry and consequences of waiting have not been established.", max_length=1000
    )


class FindingChange(StrictModel):
    field: str = Field(max_length=150)
    old_value: str = Field(max_length=500)
    new_value: str = Field(max_length=500)
    explanation: str = Field(max_length=1000)
    impact: str = Field(max_length=1000)


class Findings(StrictModel):
    schema_version: Literal["supervision.v1"] = "supervision.v1"
    category: Literal["context", "decision", "result", "error"]
    summary: str = Field(min_length=1, max_length=3000)
    recommendation: str = Field(default="", max_length=2000)
    rationale: str = Field(default="", max_length=2000)
    recommended_option_id: str | None = None
    options: list[Option] = Field(default_factory=list, max_length=5)
    sources: list[Source] = Field(default_factory=list, max_length=15)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=12)
    completed_actions: list[str] = Field(default_factory=list, max_length=12)
    disagreements: list[str] = Field(default_factory=list, max_length=12)
    changes: list[FindingChange] = Field(default_factory=list, max_length=10)
    decision: FindingDecision | None = None
    basis_message_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def option_references(self):
        ids = {o.id for o in self.options}
        if len(ids) != len(self.options):
            raise ValueError("Option IDs must be unique.")
        if self.recommended_option_id and self.recommended_option_id not in ids:
            raise ValueError("The recommended option must be supplied.")
        if self.decision and self.decision.option_id and self.decision.option_id not in ids:
            raise ValueError("The decision must refer to a supplied option.")
        return self
