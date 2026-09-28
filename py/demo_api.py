"""In-browser API for the static demo (GitHub Pages), run under Pyodide.

It mirrors the morning routes in backend/app.py over the real state machine
(backend/morning.py), so setup, the brief and Start overnight behave exactly as
they do locally. There is no worker, no adapter and no credential here: queued
instructions are saved in an in-browser database and never sent.
"""
import json
import re
from pathlib import Path

from pydantic import ValidationError

from backend.morning import Morning
from backend.morning_models import Begin, Revise, Say, SetupAnswer, Start, Steer
from backend.store import Conflict, Store

DB = Path('/demo/data/demo.sqlite3')
store = Store(DB)
morning = Morning(store)
ROUTES = {'setup': (SetupAnswer, morning.answer, 200), 'revise': (Revise, morning.revise, 200),
          'messages': (Say, morning.say, 201), 'decisions': (Steer, morning.steer, 201),
          'start': (Start, morning.start, 201)}


def snapshot():
    data = morning.snapshot()
    data['connections'] = [{'agent': a, 'state': 'demo'} for a in ('instinct', 'grok')]
    # The demo shows what each agent would receive; the real API keeps instructions server-side.
    with store.db() as c:
        for r in data['runs']:
            text = {t['id']: t['instructions'] for t in c.execute('SELECT id,instructions FROM tasks WHERE request_id=?', (r['id'],))}
            for t in r['tasks']:
                t['instructions'] = text.get(t['id'])
    return data


def route(method, path, body, key):
    if method == 'GET' and path == '/api/morning':
        return 200, snapshot()
    if method == 'POST' and path == '/api/morning':
        return 201, morning.create(key, Begin.model_validate(body).model_dump())
    m = re.fullmatch(r'/api/morning/([0-9a-f-]{36})/(\w+)', path)
    if method == 'POST' and m and m[2] in ROUTES:
        model, action, status = ROUTES[m[2]]
        return status, action(m[1], key, model.model_validate(body).model_dump())
    return 404, {'detail': 'That isn’t available in this demo.'}


def handle(method, path, body_json, key):
    """Returns (status, JSON text), like the HTTP API would."""
    try:
        status, result = route(method, path, json.loads(body_json) if body_json else None, key)
    except Conflict as e:
        status, result = 409, {'detail': str(e)}
    except KeyError:
        status, result = 404, {'detail': 'That request isn’t in this demo.'}
    except (ValidationError, ValueError):
        status, result = 422, {'detail': 'Check the answer fields and dates.'}
    return status, json.dumps(result, default=str)
