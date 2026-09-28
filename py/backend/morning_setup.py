"""Local, deterministic setup. Nothing in this module contacts an agent."""
from datetime import date
from typing import Literal
from pydantic import Field, model_validator
from backend.models import StrictModel
from backend.morning_prompts import SCOPES, money

STEPS = ('dates', 'budget', 'travel', 'stay')
QUESTIONS = {
    'dates': 'Where to, and when? Are those dates set in stone, or is there a little room to move?',
    'budget': 'What’s the budget, and what does it need to cover?',
    'travel': 'How early is too early? And how long is too long between connections?',
    'stay': 'Would you rather be close to everything, or save a little on where you stay?',
}
SCOPE_LABELS = {'whole_trip': 'the whole trip: transport, stays, food and local transit',
                'transport_and_stays': 'transport and stays only',
                'transport': 'transport between cities only'}


class Dates(StrictModel):
    start_date: date
    end_date: date
    departure_flexibility: Literal['fixed', 'flexible'] = 'fixed'
    return_flexibility: Literal['fixed', 'flexible'] = 'fixed'
    route: list[str] = Field(default=['Toronto', 'New York', 'Boston', 'Toronto'], min_length=2, max_length=8)
    travelers: int = Field(default=1, ge=1, le=9)
    comment: str = Field(default='', max_length=1500)

    @model_validator(mode='after')
    def valid(self):
        if self.end_date < self.start_date or self.end_date.year != self.start_date.year:
            raise ValueError('Use an ordered departure and return in the same explicit year.')
        if any(not x.strip() or len(x) > 100 for x in self.route):
            raise ValueError('Use named cities for the route.')
        return self


class Budget(StrictModel):
    amount: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    scope: Literal['whole_trip', 'transport_and_stays', 'transport'] = 'whole_trip'
    strictness: Literal['strict', 'target'] = 'strict'
    comment: str = Field(default='', max_length=1500)


class Travel(StrictModel):
    max_layover_hours: float = Field(ge=0, le=24, allow_inf_nan=False)
    earliest_departure: str | None = Field(default=None, pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    max_travel_hours: float | None = Field(default=None, gt=0, le=48, allow_inf_nan=False)
    overnight_buses: bool = True
    window_seat: bool = True
    extra_hours_to_save: float = Field(default=2, ge=0, le=24, allow_inf_nan=False)
    savings_threshold: float = Field(default=50, gt=0, le=100000, allow_inf_nan=False)
    comment: str = Field(default='', max_length=1500)


class Stay(StrictModel):
    location_preference: Literal['central', 'near_transit', 'flexible'] = 'near_transit'
    private_accommodation: bool = True
    window_seat: bool = True
    city_nights: dict[str, int] = Field(default_factory=dict, max_length=8)
    unsure: str = Field(default='', max_length=1500)
    comment: str = Field(default='', max_length=1500)

    @model_validator(mode='after')
    def valid(self):
        if any(not city.strip() or len(city)>100 or nights<0 or nights>60 for city,nights in self.city_nights.items()):
            raise ValueError('Use named cities and zero to sixty nights.')
        return self


MODELS = {'dates': Dates, 'budget': Budget, 'travel': Travel, 'stay': Stay}


def apply_answer(step, values, prefs):
    a = MODELS[step].model_validate(values).model_dump(mode='json')
    p = dict(prefs)
    if step == 'dates':
        p.update({k:a[k] for k in ('start_date','end_date','route','departure_flexibility','return_flexibility','travelers')})
        p['travel_year'] = int(a['start_date'][:4])
        p['date_flexibility'] = 'fixed' if a['departure_flexibility']==a['return_flexibility']=='fixed' else 'flexible'
        who = '1 traveler' if a['travelers'] == 1 else f"{a['travelers']} travelers"
        text = f"{' → '.join(a['route'])}, {who}. {a['start_date']} ({a['departure_flexibility']} departure) to {a['end_date']} ({a['return_flexibility']} return)."
        ack = 'Those dates stay put.' if p['date_flexibility']=='fixed' else 'A little room to explore; different dates still come back to you.'
    elif step == 'budget':
        p.update(currency='CAD', budget_total=a['amount'], budget_target=a['amount'],
                 budget_strictness=a['strictness'], budget_scope=a['scope'])
        covers = SCOPE_LABELS[a['scope']]
        if a['strictness'] == 'strict':
            text = f"CAD {money(a['amount'])}, a strict ceiling for {covers}."
        else:
            text = f"CAD {money(a['amount'])} target for {covers}; anything over comes back to me."
        ack = 'A ceiling, not a suggestion. Got it.' if a['strictness']=='strict' else 'We’ll aim for that. Anything over comes back to you.'
    elif step == 'travel':
        p.update({k:v for k,v in a.items() if k!='comment'})
        p['early_departures'] = a['earliest_departure'] is None or a['earliest_departure'] < '06:00'
        text = f"Layovers: {a['max_layover_hours']:g} hours maximum. Departures: {a['earliest_departure'] or 'any time'}. Travel per leg: {format(a['max_travel_hours'], 'g')+' hours maximum' if a['max_travel_hours'] else 'no specified limit'}. Overnight buses: {'allowed' if a['overnight_buses'] else 'not allowed'}. {'Window seats preferred' if a['window_seat'] else 'No seat preference'}."
        ack = f"{'No layovers' if a['max_layover_hours']==0 else format(a['max_layover_hours'], 'g')+' hours between connections, maximum'}."
    else:
        p.update({k:v for k,v in a.items() if k not in ('comment', 'unsure')})
        p['notes'] = a['unsure']
        nights = ', '.join(f'{city}: {n}' for city,n in a['city_nights'].items()) or 'allocation unresolved; propose options'
        text = f"Location: {a['location_preference'].replace('_',' ')}. {'Private stays preferred, not required' if a['private_accommodation'] else 'Shared stays acceptable'}. City nights: {nights}."
        if a['unsure']:
            text += f"\nUnsure about: {a['unsure']}"
        ack = {'central':'Close to the action, with the budget still in view.', 'near_transit':'A short walk to transit can go a long way.', 'flexible':'Room to compare where you stay.'}[a['location_preference']]
    if a['comment']:
        text += '\nComment: ' + a['comment']
    return a, p, text, ack


assert set(SCOPE_LABELS) == set(SCOPES)


MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                                      'september', 'october', 'november', 'december'], 1)}
MONTH = r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
DAY = MONTH + r'\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(20\d\d))?'


def _date(month, day, year, today):
    m = next(v for k, v in MONTHS.items() if k.startswith(month.lower()[:3]))
    y = int(year) if year else today.year
    try:
        d = date(y, m, int(day))
    except ValueError:
        return None
    if not year and d < today:
        d = d.replace(year=y + 1)
    return d


def prefill(text, today):
    """Deterministic starting values from the user's own words. Every value stays
    visible and editable in setup; nothing counts as confirmed until the user saves it."""
    import re
    found, fields = {}, []
    t = ' '.join(text.split())
    route = re.search(r'\bfrom\s+([A-Z][^,.;]*?(?:\s+to\s+[A-Z][^,.;]*?)+)(\s+and\s+back)?(?=[,.;]|\s+(?:leaving|on|in|for|from|departing)\b|$)', t)
    if route:
        cities = [c.strip() for c in re.split(r'\s+to\s+', route[1]) if c.strip()]
        if route[2] and cities[-1] != cities[0]:
            cities.append(cities[0])
        if 2 <= len(cities) <= 8 and all(len(c) <= 60 for c in cities):
            found['route'] = cities
            fields.append('route')
    dates = [(m.start(), _date(m[1], m[2], m[3], today)) for m in re.finditer(DAY, t, re.I)]
    dates = [(i, d) for i, d in dates if d]
    leave = next((d for i, d in dates if re.search(r'(leav|depart|start|out)\w*\s*(on\s+)?$', t[max(0, i - 20):i], re.I)), None)
    back = next((d for i, d in dates if re.search(r'(home|back|return\w*|until|by)\s*(on\s+|by\s+)?$', t[max(0, i - 20):i], re.I)), None)
    if not (leave and back) and len(dates) >= 2:
        leave, back = leave or dates[0][1], back or dates[1][1]
    if leave and back and back < leave:
        back = back.replace(year=leave.year + 1)
    if leave and back and back >= leave and back.year == leave.year:
        found.update(start_date=leave.isoformat(), end_date=back.isoformat(), travel_year=leave.year)
        fields.append('dates')
    money = re.search(r'(?:CAD|C\$|US\$|USD|\$)\s?(\d[\d,]*(?:\.\d+)?)\s*(k\b)?', t, re.I)
    if money:
        amount = float(money[1].replace(',', '')) * (1000 if money[2] else 1)
        if 0 < amount <= 1000000:
            found.update(budget_total=amount, budget_target=amount)
            fields.append('budget')
            window = t[max(0, money.start() - 30):money.end() + 30].lower()
            if re.search(r'\b(under|max(imum)?|no more than|at most|cap|below|up to)\b', window):
                found['budget_strictness'] = 'strict'
            if re.search(r'\b(total|all[- ]in|everything|whole trip|overall)\b', window):
                found['budget_scope'] = 'whole_trip'
            elif re.search(r'\b(flights?|transport|travel) only\b', window):
                found['budget_scope'] = 'transport'
    people = re.search(r'\b(\d|two|three|four|five|six)\s+(?:of us|people|travell?ers|adults|friends)\b', t, re.I)
    if people:
        n = {'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}.get(people[1].lower()) or int(people[1])
        if 1 <= n <= 9:
            found['travelers'] = n
            fields.append('travelers')
    if re.search(r'\bwindow seats?\b', t, re.I):
        found['window_seat'] = True
        fields.append('window_seat')
    return found, fields
