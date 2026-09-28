"""Code validates agent-reported findings. It never invents facts or calls a model."""

import json
from datetime import datetime, timezone
from decimal import Decimal

from backend.supervision_models import Findings

CATEGORIES = ("transport", "accommodation", "food", "local_transit")


def parse_findings(content):
    candidate = content.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = candidate.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        raw = json.loads(candidate)
        if not isinstance(raw, dict) or raw.get("schema_version") != "supervision.v1":
            raise ValueError()
        return Findings.model_validate(raw).model_dump(mode="json"), None
    except (ValueError, TypeError):
        return (
            None,
            "This reply could not yet be organized into structured findings. The original is preserved.",
        )


def check_options(findings, preferences, exceptions=(), instant=None):
    instant = instant or datetime.now(timezone.utc)
    checked = []
    for option in findings.get("options", []):
        costs = {c["category"]: c for c in option["costs"]}
        unknowns, violations = [], []
        complete = True
        subtotal = Decimal(0)
        for category in CATEGORIES:
            cost = costs.get(category)
            if not cost or cost.get("amount") is None:
                unknowns.append(f"{category.replace('_', ' ')} cost is unknown")
                complete = False
                continue
            if cost.get("currency") != preferences["currency"]:
                unknowns.append(
                    f"{category.replace('_', ' ')} currency is missing or differs from {preferences['currency']}"
                )
                complete = False
                continue
            subtotal += Decimal(str(cost["amount"]))
            if cost.get("taxes_included") is not True:
                unknowns.append(f"{category.replace('_', ' ')} taxes/fees are not confirmed included")
                complete = False
        total = float(subtotal) if complete else None
        scoped = [e for e in exceptions if e.get("option_id") == option["id"]]
        layover_limit = max(
            [preferences["max_layover_hours"]]
            + [e["allowed"] for e in scoped if e["field"] == "max_layover_hours"]
        )
        if option.get("layover_hours") is None:
            unknowns.append("Maximum layover is unknown")
        elif option["layover_hours"] > layover_limit:
            violations.append(
                {
                    "field": "max_layover_hours",
                    "actual": option["layover_hours"],
                    "limit": preferences["max_layover_hours"],
                    "explanation": f"{option['layover_hours']:g} hours exceeds the {preferences['max_layover_hours']:g}-hour layover limit.",
                }
            )
        budget_limit = max(
            [preferences["budget_total"]] + [e["allowed"] for e in scoped if e["field"] == "budget_total"]
        )
        if subtotal > Decimal(str(budget_limit)):
            violations.append(
                {
                    "field": "budget_total",
                    "actual": float(subtotal),
                    "limit": preferences["budget_total"],
                    "explanation": f"Reported {'full estimated' if complete else 'known partial'} costs of {preferences['currency']} {subtotal:g} already exceed the {preferences['budget_total']:g} budget.",
                }
            )
        leg_limit = preferences.get("max_travel_hours")
        if leg_limit and option.get("travel_hours") is not None and option["travel_hours"] > leg_limit:
            violations.append(
                {
                    "field": "max_travel_hours",
                    "actual": option["travel_hours"],
                    "limit": leg_limit,
                    "explanation": f"A {option['travel_hours']:g}-hour leg is longer than the {leg_limit:g}-hour limit per leg.",
                }
            )
        # An option may be one leg or stay; it only conflicts when it falls outside the trip.
        first, last = preferences.get("start_date"), preferences.get("end_date")
        for field in ("start_date", "end_date"):
            value = option.get(field)
            if not value:
                unknowns.append(f"Option {field.replace('_', ' ')} is unknown")
            elif (first and value < first) or (last and value > last):
                violations.append(
                    {
                        "field": field,
                        "actual": value,
                        "limit": f"{first} to {last}",
                        "explanation": "The option falls outside the requested travel dates. Correct the dates before selecting it.",
                    }
                )
        if option["availability"] != "available":
            unknowns.append("Availability is " + option["availability"])
        if not option.get("checked_at"):
            unknowns.append("Price/availability check time is unknown")
        expired = bool(
            option.get("expires_at")
            and datetime.fromisoformat(option["expires_at"].replace("Z", "+00:00")) <= instant
        )
        if expired:
            unknowns.append("The agent-reported offer expiry has passed; a fresh check is needed")
        checked.append(
            {
                "option_id": option["id"],
                "known_subtotal": float(subtotal),
                "total": total,
                "currency": preferences["currency"],
                "complete_costs": complete,
                "violations": violations,
                "unknowns": unknowns,
                "expired": expired,
                "scoped_exceptions": scoped,
            }
        )
    return checked


def format_instruction(include_constraints=True):
    example = {
        "schema_version": "supervision.v1",
        "category": "result",
        "summary": "Actual findings and practical impact",
        "recommendation": "Your planning recommendation, or why one is not ready",
        "rationale": "Concise user-facing rationale, not private internal reasoning",
        "recommended_option_id": None,
        "options": [],
        "sources": [],
        "assumptions": [],
        "unknowns": [],
        "completed_actions": [],
        "disagreements": [],
        "changes": [],
        "decision": None,
        "basis_message_ids": [],
    }
    return (
        "Keep the existing callback/email envelope. Put a JSON-encoded object in its STRING content field using this schema:\n"
        + json.dumps(example)
        + "\nEach option has id, label, description, costs (one per category: transport, accommodation, food, local_transit; "
        "each has category, amount or null, currency or null, taxes_included or null), layover_hours, travel_hours, "
        "start_date and end_date (YYYY-MM-DD or null), availability (available/unavailable/unknown), checked_at and expires_at "
        "(ISO timestamps with timezone, or null). Never invent missing amounts, taxes, currencies, times, or availability. "
        "Sources use url, title, optional checked_at. Include actual important alternatives. Missing costs remain unknown. "
        "Changes use field, old_value, new_value, explanation and impact as strings. Preserve mistakes and dissent. "
        "Only request user decisions for permission, genuine preference clarification or a planning selection: decision is "
        "null or {title,explanation,kind:constraint_exception/clarification/selection,option_id,consequence_of_waiting}. "
        "Do not turn researchable missing evidence into permission requests. Continue useful compliant research while exceptions wait. "
        + (
            "A six-hour layover exceeds a four-hour limit even when savings are attractive. "
            if include_constraints
            else ""
        )
        + "Agreement does not verify a source. "
        "Report only actions actually completed. Never purchase, reserve, pay, place holds, or contact providers. "
        "For combined briefs, cite the supplied stored-message IDs in basis_message_ids; retain unresolved disagreements and "
        "do not grant permissions or rewrite preferences. Give a concise explanation for the user, never hidden chain-of-thought."
    )
