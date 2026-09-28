"""Versioned outgoing prompts for Mornila.

Grok's routine wakes with a preview of the webhook body that stops at 4,000
characters (observed live: three payloads cut at exactly character 4,000 of the
compact JSON body). Every Grok instruction is therefore fitted to a byte budget
measured on the exact serialized body, with the decision placed first and lower-
priority evidence clipped before anything essential. The full brief is sent once
per confirmed version; later turns carry only compact hard limits and new context.
"""
import json
from pathlib import Path
from string import Template

PROMPT_VERSION = 'v2'
DIRECTORY = Path(__file__).with_name('prompts') / PROMPT_VERSION
PREVIEW_LIMIT = 4000
BODY_BUDGET = 3800
SCOPES = {
    'whole_trip': ('the whole trip: transport, stays, food and local transit',
                   ['transport', 'accommodation', 'food', 'local_transit']),
    'transport_and_stays': ('transport and stays only (food and local transit are separate)',
                            ['transport', 'accommodation']),
    'transport': ('transport between cities only', ['transport']),
}
CLIPPED = ' …[clipped]'


def render(name, **values):
    return Template((DIRECTORY / f'{name}.txt').read_text().rstrip('\n')).substitute(
        {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in values.items()}
    )


def clip(text, limit):
    text = '\n'.join(' '.join(line.split()) for line in str(text or '').splitlines() if line.strip())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(CLIPPED))].rstrip() + CLIPPED


def money(value):
    return f'{value:,.0f}' if float(value).is_integer() else f'{value:,.2f}'


def scope(prefs):
    return SCOPES.get(prefs.get('budget_scope') or 'whole_trip', SCOPES['whole_trip'])


def hard_limits(prefs):
    """One compact, authoritative line sent with every turn after the full brief."""
    target = prefs.get('budget_target')
    firm = 'strict ceiling' if prefs.get('budget_strictness', 'strict') == 'strict' else (
        'target; going over needs the user' if not target or target == prefs['budget_total']
        else f"hard ceiling; target {prefs.get('currency', 'CAD')} {money(target)}")
    dep, ret = prefs.get('departure_flexibility') or 'unresolved', prefs.get('return_flexibility') or 'unresolved'
    flex = f'{dep} departure and return' if dep == ret else f'{dep} departure, {ret} return'
    parts = [
        f"Budget {prefs.get('currency', 'CAD')} {money(prefs['budget_total'])} {firm}, covering {scope(prefs)[0]}",
        f"Dates {prefs.get('start_date') or 'unconfirmed'} to {prefs.get('end_date') or 'unconfirmed'}, {flex}"
        + (' (compare only; changes need review)' if 'flexible' in flex else ''),
        f"Route {' → '.join(prefs.get('route') or [])}",
        f"Travelers {prefs.get('travelers') or 1}",
        f"Layovers ≤ {prefs['max_layover_hours']:g} h",
        f"Per leg ≤ {prefs['max_travel_hours']:g} h" if prefs.get('max_travel_hours') else 'Per-leg limit unspecified',
        f"Departures from {prefs['earliest_departure']}" if prefs.get('earliest_departure') else 'Departures any time',
        'Overnight buses allowed' if prefs.get('overnight_buses', True) else 'No overnight buses',
        'Research only: no purchases, bookings, holds or provider contact',
    ]
    return '. '.join(parts) + '.'


def full_brief(prefs):
    """The complete readable brief; sent once per confirmed version to each agent."""
    nights = ', '.join(f'{c} {n}' for c, n in (prefs.get('city_nights') or {}).items()) or 'unresolved; propose a split'
    location = {'central': 'central', 'near_transit': 'near public transit',
                'flexible': 'flexible for better value'}.get(prefs.get('location_preference'), 'near public transit')
    lines = [
        hard_limits(prefs),
        f"Preferences (not hard limits): stays {location}; "
        f"{'private room preferred, not required' if prefs.get('private_accommodation', True) else 'shared stays fine'}; "
        f"{'window seats preferred' if prefs.get('window_seat', True) else 'no seat preference'}.",
        f'Nights per city: {nights}.',
    ]
    if prefs.get('notes'):
        lines.append(f"The user is unsure about / wants weighed: {prefs['notes']}")
    return '\n'.join(lines)


def grok_body(task):
    """The exact webhook body. Instructions precede the structured copy."""
    return {
        'task_id': task['id'], 'request_id': task['id'], 'conversation_id': task['root_id'],
        'constraint_version': task['constraint_version'], 'revision': task['revision'],
        'authority': 'research_only', 'instructions': transport_prompt(task),
        # Hard limits live in the instructions; this pointer avoids a second copy.
        'constraints': {'see': 'HARD LIMITS in instructions', 'brief_version': task['constraint_version']},
    }


def body_size(task):
    # Same serializer as httpx's `json=`; measured in UTF-8 bytes to stay under a character preview.
    return len(json.dumps(grok_body(task), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())


def grok_reply(task_id, version, category, fields=''):
    return render('reply_grok', task_id=task_id, version=str(version), category=category, fields=fields)


REVIEW_FIELDS = (',"review":{"decision_id":"$did","outcome":"approve|deny|redirect|defer_to_user",'
                 '"explanation":"why","uncertainty":[],"instruction":"next permitted step for Instinct","human":null}')
FINAL_FIELDS = ',"recommendation":"…","open_questions":[],"next_steps":[],"sources":[]'


def fit(task, template, fixed, flexible):
    """Render `template` so the whole Grok body fits BODY_BUDGET.

    `flexible` is [(name, text, floor)] in the order it may be shortened: the
    least important first. Fixed values (decision essentials, limits, reply
    format) are never dropped.
    """
    caps = {name: len(clip(text, 10**6)) for name, text, _ in flexible}
    for _ in range(80):
        values = {name: 'none' if not text else clip(text, caps[name]) if caps[name] >= 60 else 'omitted for space'
                  for name, text, _ in flexible}
        task['instructions'] = render(template, **fixed, **values)
        over = body_size(task) - BODY_BUDGET
        if over <= 0:
            return task['instructions']
        for name, _, floor in flexible:
            if caps[name] > floor:
                caps[name] = max(floor, caps[name] - over - 24)
                break
        else:
            break
    raise ValueError('Grok instruction cannot fit the webhook preview budget.')


def decision_block(choice, checks, scale=1.0):
    """Every field of the decision, each proportionally shortened if space is tight."""
    n = lambda base: max(60, int(base * scale))  # noqa: E731
    alternatives = choice.get('alternatives') or []
    violations = [v['explanation'] for v in (checks or {}).get('violations', [])]
    lines = [
        f"Decision: {clip(choice['title'], 160)}",
        f"Instinct recommends: {clip(choice['proposed_choice'], n(700))}",
        'Alternatives: ' + (' | '.join(f'{i}) {clip(a, n(240))}' for i, a in enumerate(alternatives, 1)) or 'none offered'),
        f"Tradeoffs: {clip(choice.get('implications'), n(600))}",
        f"Limit involved: {clip(choice.get('affected_constraint'), n(240))}",
        'Backend limit checks: ' + clip('; '.join(dict.fromkeys(violations)) if violations else 'no violation found', n(300)),
    ]
    waiting = choice.get('consequence_of_waiting')
    if waiting and waiting != 'No source-backed expiry established.':
        lines.append(f'Cost of waiting: {clip(waiting, n(200))}')
    return '\n'.join(lines)


def evidence(data):
    facts = []
    for f in data.get('facts') or []:
        bits = [f['label']]
        if f.get('travel_hours') is not None:
            bits.append(f"{f['travel_hours']:g} h travel")
        if f.get('layover_hours') is not None:
            bits.append(f"{f['layover_hours']:g} h layover")
        costs = [f"{c['category']} {c['currency'] or ''} {money(c['amount'])}".replace('  ', ' ')
                 for c in f.get('costs') or [] if c.get('amount') is not None]
        bits.append('costs ' + ', '.join(costs) if costs else 'cost unknown')
        facts.append(' · '.join(bits))
    sources = [f"{s['title']}{' (' + s['kind'] + ')' if s.get('kind') else ''} {s['url']}" for s in data.get('sources') or []]
    unknowns = data.get('unknowns') or []
    out = []
    if facts:
        out.append('Facts: ' + ' | '.join(facts))
    if sources:
        out.append('Sources: ' + ' | '.join(sources))
    if unknowns:
        out.append('Unknown: ' + ' | '.join(unknowns))
    return ' '.join(out) or 'none supplied'


def context_lines(events):
    lines = []
    for e in events:
        data = json.loads(e['content_json']) if e.get('content_json') else {}
        who = 'the user' if e.get('sender') == 'you' else 'Instinct'
        lines.append(f"- {who}: {e['text']} " + (evidence(data) if data.get('schema_version') else ''))
    return '\n'.join(lines)


def grok_review(task, decision_id, version, choice, checks, proposal, changed, deferred, prefs):
    for scale in (1, .75, .55, .4, .3, .2):
        try:
            return fit(task, 'grok_review', {
                'decision_id': decision_id, 'version': str(version), 'decision': decision_block(choice, checks, scale),
                'deferred': clip('; '.join(d['title'] for d in deferred) or 'none', 300), 'limits': hard_limits(prefs),
                'reply': grok_reply(task['id'], version, 'review', REVIEW_FIELDS.replace('$did', decision_id)),
            # Instinct's own words keep the exchange a conversation, so evidence yields
            # first; both keep a floor, and the structured decision always leads.
            }, [('changed', context_lines(changed), 0), ('evidence', evidence(proposal), 160),
                ('instinct_text', proposal.get('text'), 200)])
        except ValueError:
            continue
    raise ValueError('Grok review cannot fit the webhook preview budget.')


def grok_context(task, version, events, prefs):
    return fit(task, 'grok_context', {
        'version': str(version), 'limits': hard_limits(prefs),
        'reply': grok_reply(task['id'], version, 'acknowledgement'),
    }, [('context', context_lines(events), 400)])


def grok_initial(task, version, prefs, request, answers, confirmed):
    return fit(task, 'grok_initial', {
        'version': str(version), 'brief': full_brief(prefs), 'confirmed': confirmed,
        'reply': grok_reply(task['id'], version, 'acknowledgement'),
    }, [('answers', answers, 300), ('request', request, 300)])


def grok_final(task, version, decisions, result, unknowns, instructions, prefs):
    return fit(task, 'grok_final', {
        'version': str(version), 'limits': hard_limits(prefs),
        'decisions': clip('\n'.join(decisions), 1200) if len(decisions) > 12 else '\n'.join(decisions) or 'none',
        'reply': grok_reply(task['id'], version, 'final', FINAL_FIELDS),
    }, [('instructions', instructions, 0), ('unknowns', unknowns, 120), ('result', result, 400)])


def grok_repair(task, version, original_task, error, category, essentials, original, prefs, decision_id=None):
    fields = REVIEW_FIELDS.replace('$did', decision_id) if category == 'review' else (
        FINAL_FIELDS if category == 'final' else '')
    return fit(task, 'grok_repair', {
        'version': str(version), 'original_task': original_task, 'error': clip(error, 200), 'category': category,
        'limits': hard_limits(prefs), 'reply': grok_reply(task['id'], version, category, fields),
    }, [('original', original, 160), ('essentials', essentials, 0)])


def transport_prompt(task):
    header = f"HM-TASK: {task['id']}\nHM-CONVERSATION: {task['root_id']}\n"
    if task['agent'] == 'grok':
        # Grok instructions are complete and budget-fitted when queued.
        return header + task['instructions']
    envelope = {
        'task_id': task['id'], 'request_id': task['id'],
        'constraint_version': task['constraint_version'], 'event_id': f"{task['id']}:reply:1",
        'update_type': 'result', 'content': 'JSON-encoded good-morning.v1 object',
        'sources': [], 'requested_decision': None,
    }
    prefs = json.loads(task['constraints_json'])
    wrapper = render('instinct_wrapper', phase=task['phase'], version=str(task['constraint_version']),
                     limits=hard_limits(prefs))
    contract = render('response', envelope=json.dumps(envelope, separators=(',', ':')))
    return header + wrapper + '\n\n' + task['instructions'] + '\n\n' + contract
