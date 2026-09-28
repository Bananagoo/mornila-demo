"""Mornila state machine. No model API and no network calls in this module."""
import hashlib
import json
import re
from datetime import datetime, timedelta

from backend.findings import check_options
from backend.morning_models import Content
from backend.morning_setup import STEPS, QUESTIONS, apply_answer, prefill
from backend import morning_prompts as prompts
from backend.morning_prompts import PROMPT_VERSION, SCOPES, full_brief, render
from backend.store import Conflict, now, uid
from backend.supervision import Supervisor, dump
from backend.supervision_models import Preferences

CONTINUE_LIMIT = 3
AUTHORITY = 'Research only. No purchases, bookings, reservations, paid holds or travel-provider contact.'


def migrate(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS morning_runs (
      id TEXT PRIMARY KEY, request TEXT NOT NULL, phase TEXT NOT NULL, preference_version INTEGER NOT NULL,
      created_at TEXT NOT NULL, started_at TEXT, end_at TEXT, stop_reason TEXT, brief_event_id TEXT,
      max_cycles INTEGER NOT NULL, max_reviews INTEGER NOT NULL, hours REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS morning_preferences (
      run_id TEXT NOT NULL, version INTEGER NOT NULL, data_json TEXT NOT NULL, confirmed_at TEXT,
      PRIMARY KEY(run_id,version));
    CREATE TABLE IF NOT EXISTS morning_events (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, message_id TEXT UNIQUE, sender TEXT NOT NULL,
      category TEXT NOT NULL, text TEXT NOT NULL, content_json TEXT, processing TEXT NOT NULL,
      error TEXT, created_at TEXT NOT NULL, preference_version INTEGER NOT NULL, handled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS morning_steps (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
      link TEXT, repair_for TEXT, context_ids_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS morning_decisions (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, preference_version INTEGER NOT NULL,
      issue TEXT NOT NULL, title TEXT NOT NULL, proposal_json TEXT NOT NULL, checks_json TEXT NOT NULL,
      state TEXT NOT NULL, outcome TEXT, review_event_id TEXT, review_json TEXT, followup_id TEXT,
      correction TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS morning_setup (
      run_id TEXT PRIMARY KEY, step TEXT NOT NULL, answers_json TEXT NOT NULL, verification INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS morning_completion (
      run_id TEXT PRIMARY KEY, finish_kind TEXT, report_basis TEXT, report_task_id TEXT);
    CREATE TABLE IF NOT EXISTS morning_instructions (
      task_id TEXT PRIMARY KEY, text TEXT NOT NULL);
    ''')
    columns = {row[1] for row in c.execute('PRAGMA table_info(morning_runs)')}
    decision_columns = {row[1] for row in c.execute('PRAGMA table_info(morning_decisions)')}
    if 'user_answer' not in decision_columns:
        c.execute('ALTER TABLE morning_decisions ADD COLUMN user_answer TEXT')
    # Optional presentation wording (e.g. tidied for a demo). Originals are never changed.
    if 'display_json' not in decision_columns:
        c.execute('ALTER TABLE morning_decisions ADD COLUMN display_json TEXT')
    if 'display_json' not in {row[1] for row in c.execute('PRAGMA table_info(morning_events)')}:
        c.execute('ALTER TABLE morning_events ADD COLUMN display_json TEXT')
    if 'archived' not in columns:
        c.execute('ALTER TABLE morning_runs ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
    if 'demo' not in columns:
        # Labelled example conversations replayed with offline transports; never dispatched.
        c.execute('ALTER TABLE morning_runs ADD COLUMN demo INTEGER NOT NULL DEFAULT 0')


COMMITMENT = (r"(?:(?:travel[- ])?provider\s+contact|contact(?:ing)?(?:\s+(?:travel\s+)?providers?)?|purchas\w*|book\w*|buy\w*|"
              r"reserv\w*|pay\w*|paid\s+holds?|holds?|spend\w*)")


def positive_instruction(text):
    # Exclude explicit prohibitions without treating a whole sentence as permission.
    # "Never book, reserve or pay" and "no purchases, bookings, holds or provider contact"
    # are boundaries, not proposed commitments.
    negated = rf"\b(?:do not|don't|never|must not|cannot|can't|will not|won't|no|without)\s+(?:{COMMITMENT}\b[ ,/]*(?:(?:or|and|nor)\s+)?)+"
    return re.sub(negated, '', text, flags=re.I)


def checks_for(data, prefs):
    checks = check_options({'options': data['facts']}, prefs)
    # Text is not trusted classification. Conservative routing supplements numeric facts.
    prose = ' '.join([data['text'], dump(data.get('decision'))]).lower()
    violations = [v for check in checks for v in check['violations']]
    number = {'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
    for hit in re.finditer(r'\b(\d+(?:\.\d+)?|five|six|seven|eight|nine|ten)[ -]*(?:hour|hr)s?[ -]+(?:layover|connection)', prose):
        hours = number.get(hit[1]) or float(hit[1])
        if hours > prefs['max_layover_hours']:
            violations.append({'field': 'max_layover_hours', 'explanation': 'The reply mentions a layover beyond the confirmed limit.'})
    if re.search(r'\b(?:buy|book|reserve|purchase|pay|contact (?:the )?(?:hotel|provider|airline))\b', positive_instruction(prose)):
        # This only routes review; no outbound provider action exists.
        violations.append({'field': 'authority', 'explanation': 'Commitment language requires review; research-only authority is unchanged.'})
    for amount in re.findall(r'\bCAD\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)', prose, re.I):
        if float(amount.replace(',', '')) > prefs['budget_total']:
            violations.append({'field': 'budget_total', 'explanation': 'The reply mentions a CAD cost beyond the total budget.'})
    covered = SCOPES.get(prefs.get('budget_scope') or 'whole_trip', SCOPES['whole_trip'])
    names = '|'.join({'accommodation': 'lodging|accommodation|stays?|hotels?', 'food': 'food|meals',
                      'local_transit': 'local transit|subway|metro'}[x] for x in covered[1] if x != 'transport')
    narrowed = names and re.search(rf'\b(?:intercity )?transport[ -]only\b|\b(?:exclude|excluding|not including) (?:{names})\b|\b(?:{names}) (?:is |are )?(?:excluded|outside|not included)\b', prose)
    if narrowed:
        violations.append({'field': 'budget_scope', 'explanation': f'The confirmed budget covers {covered[0]}; its coverage cannot be narrowed.'})
    if re.search(r'\b(?:should (?:we|i)|choose between|need your (?:approval|permission|decision)|recommend (?:choosing|selecting)|exceed (?:the )?(?:budget|limit))\b', prose):
        violations.append({'field': 'unresolved_choice', 'explanation': 'The update contains a consequential choice despite its category.'})
    return {'options': checks, 'violations': violations}


class Morning:
    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings
        self.submissions = Supervisor(store)

    def get(self, c, rid):
        r = c.execute('SELECT * FROM morning_runs WHERE id=?', (rid,)).fetchone()
        if not r:
            raise KeyError(rid)
        return dict(r)

    def prefs(self, c, r):
        return json.loads(c.execute('SELECT data_json FROM morning_preferences WHERE run_id=? AND version=?',
                                   (r['id'], r['preference_version'])).fetchone()[0])

    def boundary(self, c, r):
        prefs = Preferences.model_validate(self.prefs(c, r)).model_dump(mode='json')
        return {**prefs, 'authority': AUTHORITY,
                'includes': SCOPES[prefs['budget_scope']][1]}

    def event(self, c, r, sender, category, text, task_id=None, content=None):
        eid = uid()
        c.execute('INSERT INTO morning_events (id,run_id,task_id,message_id,sender,category,text,content_json,processing,error,created_at,preference_version,handled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)',
                  (eid, r['id'], task_id, None, sender, category, text,
                   dump(content) if content else None, 'valid', None, now(), r['preference_version']))
        return eid

    def transcript(self, c, r):
        return [dict(e) for e in c.execute(
            "SELECT id,sender,category,text,created_at,preference_version FROM morning_events WHERE run_id=? ORDER BY created_at,rowid", (r['id'],))]

    def queue(self, c, r, agent, kind, instruction, token, link=None, repair_for=None, context_ids=()):
        """Persist one outgoing instruction before dispatch.

        Grok instructions are builders: they are rendered here, with the real task
        identity, and fitted to the webhook preview budget.
        """
        stepid = f"{r['id']}:{r['preference_version']}:{token}"
        old = c.execute('SELECT task_id FROM morning_steps WHERE id=?', (stepid,)).fetchone()
        if old:
            return old[0]
        previous = c.execute('SELECT * FROM tasks WHERE request_id=? AND agent=? ORDER BY revision DESC LIMIT 1',
                             (r['id'], agent)).fetchone()
        tid, at = uid(), now()
        root = previous['root_id'] if previous else tid
        revision = previous['revision'] + 1 if previous else 1
        if callable(instruction):
            instruction = instruction({'id': tid, 'root_id': root, 'agent': agent, 'revision': revision,
                                       'constraint_version': r['preference_version'],
                                       'constraints_json': dump(self.boundary(c, r)), 'phase': 'gm_' + kind})
        c.execute('''INSERT INTO tasks
        (id,root_id,parent_id,agent,instructions,constraints_json,constraint_version,revision,purpose,
        created_at,rfc_message_id,group_id,origin,request_id,request_revision,phase,repair_for)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (tid, root, previous['id'] if previous else None, agent, instruction, dump(self.boundary(c, r)),
                   r['preference_version'], revision, 'research', at, f'<hm.{tid}@morning-brief.local>',
                   r['id'], 'good_morning', r['id'], r['preference_version'], 'gm_' + kind, repair_for))
        c.execute('''INSERT INTO messages (id,task_id,agent,provider_message_id,rfc_message_id,direction,received_at,content,raw_json)
                     VALUES (?,?,?,?,?,'out',?,?,?)''',
                  (uid(), tid, agent, tid, f'<hm.{tid}@morning-brief.local>', at, instruction,
                   dump({'instruction': instruction, 'prompt_version': PROMPT_VERSION})))
        c.execute('INSERT INTO morning_steps VALUES (?,?,?,?,?,?,?)',
                  (stepid, r['id'], tid, kind, link, repair_for, dump(list(context_ids))))
        return tid

    def answers(self, c, r):
        """The user's latest setup answers, attributed to their questions."""
        latest = {}
        for e in c.execute("SELECT text,content_json FROM morning_events WHERE run_id=? AND category='setup_answer' ORDER BY created_at,rowid", (r['id'],)):
            latest[json.loads(e['content_json'])['step']] = e['text'].removeprefix('Correction: ')
        return '\n'.join(f'Q (setup): {QUESTIONS[s]}\nA (user): {latest[s]}' for s in STEPS if s in latest)

    def setup_question(self, c, r, step):
        self.event(c, r, 'setup', 'setup_question', QUESTIONS[step], content={'step': step})

    def create(self, key, data):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.create', data])
            if old:
                return old
            rid, at = uid(), now()
            prefs = Preferences().model_dump(mode='json')
            defaults = c.execute('SELECT preferences_json FROM preference_defaults WHERE id=1').fetchone()
            if defaults:
                prefs = Preferences.model_validate_json(defaults[0]).model_dump(mode='json')
            prefs['currency'] = 'CAD'
            # Visible, editable starting values read from the request; nothing is confirmed until setup.
            found, prefilled = prefill(data['text'], datetime.now().date())
            prefs = Preferences.model_validate({**prefs, **found}).model_dump(mode='json')
            limits = self.settings
            c.execute('''INSERT INTO morning_runs (id,request,phase,preference_version,created_at,started_at,end_at,
                         stop_reason,brief_event_id,max_cycles,max_reviews,hours) VALUES (?,?,'setup',1,?,NULL,NULL,NULL,NULL,?,?,?)''',
                      (rid, data['text'], at, max(1, min(3, getattr(limits, 'morning_max_cycles', 3))),
                       1 if data.get('verification') else max(1, min(10, getattr(limits, 'morning_max_reviews', 10))),
                       min(data.get('hours', 8), 1/6 if data.get('verification') else 8, max(.05, min(8, getattr(limits, 'morning_hours', 8))))))
            c.execute('INSERT INTO morning_preferences VALUES (?,1,?,NULL)', (rid, dump(prefs)))
            c.execute('INSERT INTO morning_setup VALUES (?,?,?,?)', (rid, 'dates', '{}', int(data.get('verification', False))))
            r = self.get(c, rid)
            self.event(c, r, 'you', 'request', data['text'], content={'prefilled': prefilled} if prefilled else None)
            self.event(c, r, 'setup', 'setup', 'Let’s give your agents a good starting point.')
            self.setup_question(c, r, 'dates')
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def answer(self, rid, key, data):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.answer', rid, data])
            if old:
                return old
            r = self.get(c, rid)
            if r['phase'] != 'setup' or data['preference_version'] != r['preference_version']:
                raise Conflict('Setup changed. Refresh and check the current answers; your draft is kept.')
            saved = c.execute('SELECT * FROM morning_setup WHERE run_id=?', (rid,)).fetchone()
            answers = json.loads(saved['answers_json'])
            step = data['step']
            if step != saved['step'] and step not in answers:
                raise Conflict('Answer the current question first.')
            answer, prefs, text, ack = apply_answer(step, data['answer'], self.prefs(c, r))
            prefs = Preferences.model_validate(prefs).model_dump(mode='json')
            if prefs['city_nights'] and prefs['start_date'] and prefs['end_date']:
                nights = (datetime.fromisoformat(prefs['end_date']) - datetime.fromisoformat(prefs['start_date'])).days
                if sum(prefs['city_nights'].values()) > nights:
                    raise Conflict('The allocated nights exceed the trip. Adjust the nights or dates.')
            correction = step in answers
            answers[step] = answer
            r['preference_version'] += 1
            next_step = next((x for x in STEPS if x not in answers), 'review')
            c.execute('UPDATE morning_runs SET preference_version=?,brief_event_id=NULL WHERE id=?', (r['preference_version'], rid))
            c.execute('INSERT INTO morning_preferences VALUES (?,?,?,NULL)', (rid, r['preference_version'], dump(prefs)))
            c.execute('UPDATE morning_setup SET step=?,answers_json=? WHERE run_id=?', (next_step, dump(answers), rid))
            self.event(c, r, 'you', 'setup_answer', ('Correction: ' if correction else '') + text, content={'step': step, 'answer': answer})
            self.event(c, r, 'setup', 'setup', ack)
            if next_step == 'review':
                eid = self.event(c, r, 'setup', 'setup_brief', 'One quick check before we hand this over.', content={'brief': prefs})
                c.execute('UPDATE morning_runs SET brief_event_id=? WHERE id=?', (eid, rid))
            elif not correction:
                self.setup_question(c, r, next_step)
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def revise(self, rid, key, data):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.revise', rid, data])
            if old:
                return old
            r = self.get(c, rid)
            if r['demo']:
                raise Conflict('This is an example conversation. Start a new request to plan your own trip.')
            if data['preference_version'] != r['preference_version']:
                raise Conflict('The brief changed; refresh before editing.')
            if c.execute("SELECT 1 FROM tasks WHERE request_id=? AND delivery IN ('sending','uncertain')", (rid,)).fetchone():
                raise Conflict('Resolve the delivery in progress before changing the brief.')
            prefs = Preferences.model_validate(self.prefs(c, r)).model_dump(mode='json')
            r['preference_version'] += 1
            c.execute("UPDATE morning_runs SET phase='setup',preference_version=?,brief_event_id=NULL,stop_reason=NULL WHERE id=?", (r['preference_version'], rid))
            c.execute('INSERT INTO morning_preferences VALUES (?,?,?,NULL)', (rid, r['preference_version'], dump(prefs)))
            c.execute("INSERT INTO morning_setup (run_id,step,answers_json) VALUES (?,'dates','{}') ON CONFLICT(run_id) DO UPDATE SET step='dates',answers_json='{}'", (rid,))
            c.execute("UPDATE tasks SET delivery='superseded' WHERE request_id=? AND delivery='queued'", (rid,))
            c.execute("UPDATE morning_decisions SET state='outdated' WHERE run_id=?", (rid,))
            c.execute('DELETE FROM morning_completion WHERE run_id=?', (rid,))
            self.event(c, r, 'setup', 'setup', 'Let’s check the brief again. Earlier messages are kept below.')
            self.setup_question(c, r, 'dates')
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def say(self, rid, key, data):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.say', rid, data])
            if old:
                return old
            r = self.get(c, rid)
            if r['demo']:
                raise Conflict('This is an example conversation. Start a new request to message the agents.')
            if data['preference_version'] != r['preference_version']:
                raise Conflict('The brief changed. Refresh before sending; your draft is preserved.')
            if r['phase'] in ('setup','onboarding'):
                raise Conflict('Complete the local setup and Start overnight before messaging Instinct.')
            if c.execute("SELECT 1 FROM tasks WHERE request_id=? AND delivery IN ('queued','sending','uncertain')", (rid,)).fetchone():
                raise Conflict('An instruction is still being delivered. Your draft is kept.')
            r = self.reopen(c, r)
            about = ''
            if data.get('decision_id'):
                d = c.execute("SELECT * FROM morning_decisions WHERE id=? AND run_id=? AND state='deferred'",
                              (data['decision_id'], rid)).fetchone()
                if not d:
                    raise Conflict('That choice is no longer waiting for you. Refresh to see its current state.')
                review = json.loads(d['review_json'] or '{}').get('human') or {}
                about = (f"It answers the choice Grok left for the user: “{d['title']}”. Question: {review.get('question', d['title'])} "
                         f"Options offered: {'; '.join(review.get('options') or [])}. Apply the answer to that choice only.")
                c.execute("UPDATE morning_decisions SET state='answered',user_answer=? WHERE id=?", (data['text'], d['id']))
            eid = self.event(c, r, 'you', 'instruction', data['text'])
            tid = self.queue(c, r, 'instinct', 'manual', render('manual', text=data['text'], about=about), f'user:{eid}')
            c.execute('INSERT INTO morning_instructions VALUES (?,?)', (tid, data['text']))
            c.execute('UPDATE morning_events SET task_id=? WHERE id=?', (tid, eid))
            c.execute("UPDATE morning_runs SET phase='running' WHERE id=?", (rid,))
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def reopen(self, c, r):
        """A morning reply reopens research under the same confirmed brief."""
        if not (r['phase'] == 'finished' or (r['end_at'] and r['end_at'] <= now())):
            return r
        setup = c.execute('SELECT verification FROM morning_setup WHERE run_id=?', (r['id'],)).fetchone()
        if setup and setup['verification']:
            raise Conflict('This bounded verification has ended and cannot be extended.')
        end = (datetime.fromisoformat(now()) + timedelta(hours=r['hours'])).isoformat()
        c.execute("UPDATE morning_runs SET phase='running',end_at=?,stop_reason=NULL WHERE id=?", (end, r['id']))
        c.execute('DELETE FROM morning_completion WHERE run_id=?', (r['id'],))
        r = self.get(c, r['id'])
        self.event(c, r, 'system', 'reopen', f'Research reopened for up to {r["hours"]:g} hours with the same brief.')
        return r

    def steer(self, rid, key, data):
        """The user's approve / deny / redirect on one decision. Waiting decisions accept all
        three; decisions the agents settled on their own can be overridden (deny or redirect)."""
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.steer', rid, data])
            if old:
                return old
            r = self.get(c, rid)
            if r['demo']:
                raise Conflict('This is an example conversation, so nothing is sent.')
            if data['preference_version'] != r['preference_version']:
                raise Conflict('The brief changed. Refresh before deciding.')
            d = c.execute('SELECT * FROM morning_decisions WHERE id=? AND run_id=? AND preference_version=?',
                          (data['decision_id'], rid, r['preference_version'])).fetchone()
            action, note = data['action'], (data.get('note') or '').strip()
            allowed = {'deferred': ('approve', 'deny', 'redirect'), 'resolved': ('deny', 'redirect'),
                       'answered': ('deny', 'redirect')}
            if not d or action not in allowed.get(d['state'], ()):
                raise Conflict('That decision can’t take this action now. Refresh to see its current state.')
            if action == 'redirect' and not note:
                raise Conflict('Say what Instinct should do instead.')
            if c.execute("SELECT 1 FROM tasks WHERE request_id=? AND delivery IN ('queued','sending','uncertain')", (rid,)).fetchone():
                raise Conflict('An instruction is still being delivered. Try again in a moment.')
            r = self.reopen(c, r)
            proposal, checks = json.loads(d['proposal_json']), json.loads(d['checks_json'])
            limits = sorted({v['explanation'] for v in checks.get('violations', [])})
            verdict = {'approve': f"approved. {proposal['proposed_choice']}",
                       'deny': f"declined. Drop this: {proposal['proposed_choice']}",
                       'redirect': f'redirected. Do this instead: {note}'}[action]
            scope = ('It is an explicit exception for this choice only (' + '; '.join(limits) + '); every standing limit stays as confirmed.'
                     if action == 'approve' and limits else 'Every standing limit stays as confirmed.')
            overriding = d['state'] != 'deferred'
            detail = (f'This overrides what the agents settled overnight ({d["outcome"]}). ' if overriding else '') + (
                f'Their note: {note}' if note and action != 'redirect' else '')
            label = {'approve': 'Approved', 'deny': 'Declined', 'redirect': 'Redirected'}[action]
            text = f'{label}: {d["title"]}' + (f' — {note}' if note else '')
            state = {'approve': 'user_approved', 'deny': 'user_denied', 'redirect': 'user_redirected'}[action]
            c.execute('UPDATE morning_decisions SET state=?,user_answer=? WHERE id=?', (state, text, d['id']))
            eid = self.event(c, r, 'you', 'steer', text, content={'decision_id': d['id'], 'action': action, 'note': note})
            tid = self.queue(c, r, 'instinct', 'steer', render('steer', title=d['title'], verdict=verdict,
                             detail=detail.strip(), scope=scope), f'steer:{eid}')
            c.execute('INSERT INTO morning_instructions VALUES (?,?)', (tid, text))
            c.execute('UPDATE morning_events SET task_id=? WHERE id=?', (tid, eid))
            c.execute("UPDATE morning_runs SET phase='running' WHERE id=?", (rid,))
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def exceptions(self, c, r):
        """Limit fields the user explicitly approved an exception for, per choice."""
        fields = set()
        for (issue,) in c.execute("SELECT issue FROM morning_decisions WHERE run_id=? AND preference_version=? AND state='user_approved'",
                                  (r['id'], r['preference_version'])):
            fields.update(issue.split('|'))
        return fields

    def start(self, rid, key, data):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            fp, old = self.submissions.prior_submission(c, key, ['morning.start', rid, data])
            if old:
                return old
            r = self.get(c, rid)
            setup = c.execute('SELECT * FROM morning_setup WHERE run_id=?', (rid,)).fetchone()
            if not setup or setup['step'] != 'review' or not r['brief_event_id'] or r['phase'] != 'setup' or data['preference_version'] != r['preference_version'] or data['brief_event_id'] != r['brief_event_id']:
                raise Conflict('Complete and confirm the current local brief before starting.')
            prefs = Preferences.model_validate(self.prefs(c, r)).model_dump(mode='json')
            if not prefs['travel_year'] or not prefs['start_date'] or not prefs['end_date']:
                raise Conflict('Confirm the year and dates before starting.')
            at = now()
            c.execute("UPDATE morning_runs SET phase='running',started_at=?,end_at=?,stop_reason=NULL WHERE id=?", (at, (datetime.fromisoformat(at) + timedelta(hours=r['hours'])).isoformat(), rid))
            c.execute('UPDATE morning_preferences SET confirmed_at=? WHERE run_id=? AND version=?', (at, rid, r['preference_version']))
            self.event(c, r, 'you', 'confirmation', 'Confirmed this brief. Start overnight; research only.')
            transcript = self.transcript(c, r)
            # The full brief goes to each agent once per confirmed version. Later
            # turns carry compact hard limits and only what is new.
            brief, answers = self.boundary(c, r), self.answers(c, r)
            self.queue(c, r, 'grok', 'initial', lambda t: prompts.grok_initial(
                t, r['preference_version'], brief, r['request'], answers, at[:10]), 'initial',
                context_ids=[e['id'] for e in transcript])
            self.queue(c, r, 'instinct', 'research', render('research', version=str(r['preference_version']),
                       request=r['request'], brief=full_brief(brief), answers=answers), 'research')
            self.event(c, r, 'system', 'start', 'Overnight work saved. Delivery and replies will appear here.')
            return self.submissions.record_submission(c, key, fp, {'id': rid})

    def receive(self, c, task, mid, update, error):
        r = self.get(c, task['request_id'])
        m = c.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
        step = c.execute('SELECT * FROM morning_steps WHERE task_id=?', (task['id'],)).fetchone()
        originating_id = step['repair_for'] or task['id']
        original = update['content'] if update else m['content']
        data = None
        processing = 'valid'
        category = 'unprocessed'
        text = original
        if update and update['update_type'] in ('acknowledgement', 'error'):
            category = 'acknowledgement' if update['update_type'] == 'acknowledgement' else 'error'
        else:
            try:
                if error:
                    raise ValueError()
                candidate = original.strip()
                if candidate.startswith('```') and candidate.endswith('```'):
                    candidate = candidate.split('\n', 1)[1].rsplit('```', 1)[0]
                data = Content.model_validate_json(candidate).model_dump(mode='json')
                if update and (update.get('constraint_version') not in (None, task['constraint_version']) or update.get('request_id') not in (None, task['id'])):
                    raise ValueError()
                category, text = data['category'], data['text']
                effective = step['kind']
                if effective == 'repair':
                    effective = c.execute('SELECT kind FROM morning_steps WHERE task_id=?', (step['repair_for'],)).fetchone()[0]
                allowed = {'onboarding': {'onboarding', 'acknowledgement'}, 'initial': {'acknowledgement', 'context'},
                           'context': {'acknowledgement', 'context'}, 'review': {'review', 'acknowledgement'},
                           'final': {'final', 'acknowledgement'}}
                if category not in allowed.get(effective, {'context', 'decision_required', 'final', 'acknowledgement'}):
                    raise ValueError()
                if category == 'review' and (not data['review'] or data['review']['decision_id'] != step['link']):
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                data, processing, category, text = None, 'malformed', 'unprocessed', original
                # Name the actual problem so the one allowed repair can fix it.
                detail = '; '.join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in exc.errors()[:3]) \
                    if hasattr(exc, 'errors') else ''
                error = 'Reply retained; ' + (f'fix: {detail}.' if detail else 'format or correlation needs correction.')
        stale = bool(m['stale'] or task['constraint_version'] != r['preference_version'])
        if category not in ('context', 'acknowledgement', 'error'):
            previous = c.execute('''SELECT 1 FROM morning_events e JOIN morning_steps s ON s.task_id=e.task_id
                WHERE (s.task_id=? OR s.repair_for=?) AND e.sender!='you' AND e.processing='valid'
                AND e.category IN ('decision_required','review','final','onboarding')
                AND (e.category=? OR ?='unprocessed')''',
                (originating_id, originating_id, category, category)).fetchone()
            stale = stale or bool(previous)
        if data and ((category == 'onboarding' and r['phase'] != 'onboarding') or
                     (category not in ('onboarding', 'acknowledgement') and r['phase'] == 'onboarding' and step['kind'] != 'onboarding')):
            stale = True
        if not stale and (r['phase'] == 'finished' or (r['phase'] not in ('setup','onboarding') and r['end_at'] and r['end_at'] <= now())):
            processing = 'late'
        if stale:
            processing = 'stale'
            c.execute('UPDATE messages SET stale=1 WHERE id=?', (mid,))
            c.execute('UPDATE tasks SET latest_message_id=?,work=?,completed_at=?,acknowledged_at=? WHERE id=? AND latest_message_id=?',
                      (task['latest_message_id'], task['work'], task['completed_at'], task['acknowledged_at'], task['id'], mid))
        elif processing == 'malformed':
            c.execute("UPDATE tasks SET work='waiting',completed_at=NULL,warning=? WHERE id=?",
                      ('Reply format needs correction; requested work is not yet confirmed complete.', task['id']))
        elif processing == 'valid' and data and category not in ('acknowledgement', 'error'):
            # A repair and its source are one response opportunity. A delayed original
            # cannot open another decision, and a successful repair clears the old wait.
            if step['repair_for']:
                c.execute('UPDATE tasks SET work=?,completed_at=?,warning=NULL WHERE id=?',
                          ('completed' if update and update['update_type'] == 'result' else 'acknowledged',
                           m['received_at'] if update and update['update_type'] == 'result' else None, originating_id))
            else:
                c.execute("UPDATE tasks SET delivery='superseded',warning='Original reply recovered before repair dispatch.' WHERE repair_for=? AND delivery='queued'", (task['id'],))
            c.execute('UPDATE tasks SET warning=NULL WHERE id=?', (task['id'],))
            c.execute('''UPDATE morning_events SET processing='repaired' WHERE processing='malformed'
                AND task_id IN (SELECT task_id FROM morning_steps WHERE task_id=? OR repair_for=?)''',
                (originating_id, originating_id))
        eid = uid()
        c.execute('INSERT INTO morning_events (id,run_id,task_id,message_id,sender,category,text,content_json,processing,error,created_at,preference_version,handled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)',
                  (eid, r['id'], task['id'], mid, task['agent'], category, text,
                   dump(data) if data else None, processing, error, m['received_at'], task['constraint_version']))
        if not stale and processing == 'valid' and category == 'onboarding':
            c.execute('UPDATE morning_runs SET brief_event_id=? WHERE id=?',
                      (eid if data and data['ready'] and data['brief'] else None, r['id']))

    def archive(self, rid, archived=True):
        """Hide a request from history without deleting anything. Active work is stopped first."""
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self.get(c, rid)
            if archived and r['phase'] in ('onboarding', 'running', 'reporting'):
                self.stop(c, r, 'Archived by the owner. Received results are kept.')
            c.execute('UPDATE morning_runs SET archived=? WHERE id=?', (int(archived), rid))

    def stop(self, c, r, reason, kind='stopped'):
        c.execute("UPDATE morning_runs SET phase='finished',stop_reason=? WHERE id=?", (reason, r['id']))
        # Queued work is cancellable; accepted instructions remain in history and may still reply.
        c.execute("UPDATE tasks SET delivery='superseded',warning=? WHERE request_id=? AND delivery='queued'", (reason, r['id']))
        c.execute('INSERT INTO morning_completion (run_id,finish_kind) VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET finish_kind=excluded.finish_kind', (r['id'], kind))
        self.event(c, r, 'system', 'stop', reason)

    def context(self, c, r):
        known = set()
        for step in c.execute('SELECT context_ids_json FROM morning_steps WHERE run_id=?', (r['id'],)):
            known.update(json.loads(step[0]))
        # Instinct's findings plus the user's own morning decisions, each forwarded to Grok once.
        rows = c.execute('''SELECT id,sender,text,content_json FROM morning_events WHERE run_id=? AND processing='valid'
            AND preference_version=? AND (sender='instinct' OR (sender='you' AND category IN ('steer','instruction')))
            ORDER BY created_at,rowid''', (r['id'], r['preference_version']))
        return [dict(e) for e in rows if e['id'] not in known]

    def deferred(self, c, r):
        return [dict(d) for d in c.execute("SELECT id,issue,title FROM morning_decisions WHERE run_id=? AND preference_version=? AND state='deferred'", (r['id'], r['preference_version']))]

    def review_decision(self, c, r, e, data):
        checks = checks_for(data, self.prefs(c, r))
        choice = data.get('decision')
        excepted = self.exceptions(c, r)
        if excepted:
            # A limit the user already made an exception for is not re-raised for the same choice.
            checks['violations'] = [v for v in checks['violations'] if v['field'] not in excepted]
        if not choice and not checks['violations']:
            return False
        if not choice:
            titles = {'budget_total': 'Over the budget ceiling', 'budget_scope': 'Budget coverage changed',
                      'max_layover_hours': 'Layover beyond your limit', 'max_travel_hours': 'A leg longer than your limit',
                      'authority': 'Possible booking language', 'unresolved_choice': 'A choice inside an update',
                      'start_date': 'Dates outside your trip', 'end_date': 'Dates outside your trip'}
            field = next((v['field'] for v in checks['violations'] if v['field'] in titles), None)
            choice = {'issue': 'boundary-check', 'title': titles.get(field, 'Something to check against your limits'),
                      'proposed_choice': data['text'], 'action': 'research', 'option_id': None, 'alternatives': [],
                      'implications': 'This update may cross one of your confirmed limits.',
                      'affected_constraint': 'See the limit checks',
                      'consequence_of_waiting': 'No source-backed expiry established.'}
        # Server-generated constraint identity groups repeat arguments even if the model renames them.
        fields = sorted({v['field'] for v in checks['violations']})
        issue = '|'.join(fields) if fields else re.sub(r'[^a-z0-9]+', '-', choice['issue'].lower())
        deferred = self.deferred(c, r)
        if any(d['issue'] == issue for d in deferred):
            c.execute("UPDATE morning_events SET processing='deferred_repeat',error='This choice already awaits your input; no new review was started.' WHERE id=?", (e['id'],))
            return True
        count = c.execute('SELECT COUNT(*) FROM morning_decisions WHERE run_id=?', (r['id'],)).fetchone()[0]
        cycles = c.execute('SELECT COUNT(*) FROM morning_decisions WHERE run_id=? AND preference_version=? AND issue=?', (r['id'], r['preference_version'], issue)).fetchone()[0]
        if count >= r['max_reviews'] or cycles >= r['max_cycles']:
            self.stop(c, r, 'Automatic review limit reached. Partial results are saved; further research needs your direction.')
            return True
        did = uid()
        c.execute('''INSERT INTO morning_decisions (id,run_id,event_id,preference_version,issue,title,proposal_json,checks_json,
                     state,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?)''',
                  (did, r['id'], e['id'], r['preference_version'], issue, choice['title'], dump(choice), dump(checks), now()))
        context = self.context(c, r)
        brief = self.boundary(c, r)
        self.queue(c, r, 'grok', 'review', lambda t: prompts.grok_review(
            t, did, r['preference_version'], choice, checks, data, [x for x in context if x['id'] != e['id']], deferred, brief),
            f'review:{did}', link=did, context_ids=[x['id'] for x in context])
        return True

    def apply_review(self, c, r, e, data):
        result = data['review']
        d = c.execute('SELECT * FROM morning_decisions WHERE id=? AND run_id=?', (result['decision_id'], r['id'])).fetchone()
        if not d or d['state'] != 'pending' or d['preference_version'] != r['preference_version']:
            c.execute("UPDATE morning_events SET processing='stale',error='This review no longer matches a pending decision.' WHERE id=?", (e['id'],))
            return
        checks, choice = json.loads(d['checks_json']), json.loads(d['proposal_json'])
        correction = None
        result = dict(result)
        proposed_instruction = result['instruction'] + ' ' + (result.get('human') or {}).get('meanwhile', '')
        unsafe = bool(re.search(r'\b(?:book|buy|purchase|reserve|pay|contact|increase (?:the )?budget|ignore (?:the )?limit)\b', positive_instruction(proposed_instruction), re.I))
        unsafe = unsafe or bool(checks_for({'facts': [], 'text': proposed_instruction}, self.prefs(c, r))['violations'])
        unknown_plan = choice['action'] == 'plan' and (not checks['options'] or any(x['unknowns'] for x in checks['options']))
        if unsafe or (result['outcome'] == 'approve' and (checks['violations'] or unknown_plan)):
            correction = 'Mornila held Grok’s answer because it wasn’t clearly within your confirmed limits. Grok’s original review is kept.'
            result['outcome'] = 'defer_to_user'
            result['human'] = {'question': choice['title'], 'why': correction, 'options': choice['alternatives'],
                               'meanwhile': 'Research compliant alternatives within all confirmed limits. Leave this choice unselected.'}
            result['instruction'] = result['human']['meanwhile']
        outcome = result['outcome']
        instruction = result['human']['meanwhile'] if outcome == 'defer_to_user' else result['instruction']
        # Deterministic outcome semantics cannot be negated by freeform agent wording.
        prefixes = {'approve': 'Continue only the proposed within-scope research. ',
                    'deny': 'Exclude the proposed choice from the requested plan. ',
                    'redirect': 'Change the research direction as described, preserving all hard limits. ',
                    'defer_to_user': 'Leave this choice unselected. Keep the current hard limits. Do not repeat it without new human input. '}
        if outcome == 'defer_to_user':
            c.execute("UPDATE morning_decisions SET state='deferred' WHERE id=?", (d['id'],))
        if correction:
            self.event(c, r, 'system', 'correction', 'Grok’s proposed resolution was held by a boundary check. This choice still needs you; the confirmed limits are unchanged.')
        applied_instruction = prefixes[outcome] + instruction
        followup = self.queue(c, r, 'instinct', 'relay', render('relay', decision_id=d['id'], outcome=outcome,
                             said=e['text'], review=result, instruction=applied_instruction,
                             deferred=self.deferred(c, r)), f'relay:{d["id"]}', link=d['id'])
        c.execute('INSERT OR IGNORE INTO morning_instructions VALUES (?,?)', (followup, applied_instruction))
        c.execute('UPDATE morning_decisions SET outcome=?,review_event_id=?,review_json=?,followup_id=?,correction=? WHERE id=?',
                  (outcome, e['id'], dump(result), followup, correction, d['id']))

    def obligations(self, c, r, include_report=True):
        """Read durable obligations, never infer completion from provider acceptance."""
        tasks = c.execute('SELECT t.*,s.kind FROM tasks t JOIN morning_steps s ON s.task_id=t.id WHERE t.request_id=? AND t.constraint_version=?', (r['id'], r['preference_version'])).fetchall()
        pending = []
        for t in tasks:
            if t['delivery'] == 'superseded' or (t['kind'] == 'final' and not include_report):
                continue
            if t['delivery'] != 'sent':
                pending.append(dict(t))
                continue
            kind = t['kind']
            if kind == 'repair':
                original = c.execute('SELECT kind FROM morning_steps WHERE task_id=?', (t['repair_for'],)).fetchone()
                kind = original[0] if original else kind
            if kind in ('initial','context'):
                continue  # Context requires delivery, never a conversational answer.
            response = c.execute("""SELECT 1 FROM morning_events e JOIN morning_steps s ON s.task_id=e.task_id
                WHERE (s.task_id=? OR s.repair_for=?) AND e.processing='valid'
                AND e.sender IN ('instinct','grok') AND e.category NOT IN ('acknowledgement','error')""", (t['id'], t['id'])).fetchone()
            if not response:
                pending.append(dict(t))
        return pending

    def keep_going(self, c, r):
        """Ask Instinct to continue when its latest answer was only an interim update and
        nothing else is outstanding; otherwise a run can idle until its deadline with no
        summary. Bounded per brief version; the last ask requests the final report."""
        if r['phase'] != 'running':
            return
        if c.execute("SELECT 1 FROM morning_decisions WHERE run_id=? AND preference_version=? AND state='pending'",
                     (r['id'], r['preference_version'])).fetchone() or self.obligations(c, r, include_report=False):
            return
        latest = c.execute('''SELECT e.category, e.task_id, COALESCE(s.repair_for, e.task_id) AS origin FROM morning_events e
            JOIN morning_steps s ON s.task_id=e.task_id WHERE e.run_id=? AND e.preference_version=? AND e.sender='instinct'
            AND e.processing='valid' AND e.category NOT IN ('acknowledgement','error') ORDER BY e.created_at DESC, e.rowid DESC''',
            (r['id'], r['preference_version'])).fetchone()
        if not latest or latest['category'] != 'context':
            return
        kind = c.execute('SELECT kind FROM morning_steps WHERE task_id=?', (latest['origin'],)).fetchone()
        if not kind or kind[0] not in ('research', 'relay', 'manual', 'continue', 'steer'):
            return
        asked = c.execute("SELECT COUNT(*) FROM morning_steps WHERE run_id=? AND kind='continue' AND id LIKE ?",
                          (r['id'], f"{r['id']}:{r['preference_version']}:%")).fetchone()[0]
        if asked >= CONTINUE_LIMIT:
            return
        ask = ('this is the last check-in, so please send final now with what you have.' if asked == CONTINUE_LIMIT - 1
               else 'take the next useful research step toward a complete plan.')
        self.queue(c, r, 'instinct', 'continue', render('continue', ask=ask,
                   deferred='; '.join(d['title'] for d in self.deferred(c, r)) or 'none'), f"continue:{latest['origin']}")

    def settle(self, c, r):
        if r['phase'] not in ('running','reporting'):
            return
        pending_decisions = c.execute("SELECT 1 FROM morning_decisions WHERE run_id=? AND preference_version=? AND state='pending'", (r['id'], r['preference_version'])).fetchone()
        if pending_decisions or self.obligations(c, r, include_report=False):
            return
        intent = c.execute("SELECT * FROM morning_events WHERE run_id=? AND preference_version=? AND sender='instinct' AND processing='valid' ORDER BY created_at DESC,rowid DESC", (r['id'], r['preference_version'])).fetchall()
        if not any(e['category']=='final' or (json.loads(e['content_json'] or '{}').get('stop_reason')) for e in intent):
            return
        # A report is valid only for the evidence/decisions available when requested.
        evidence = [dict(e) for e in c.execute("SELECT id,sender,category,text,content_json FROM morning_events WHERE run_id=? AND preference_version=? AND processing='valid' AND sender IN ('instinct','grok','you') AND category NOT IN ('acknowledgement','error') AND NOT (sender='grok' AND category='final') ORDER BY created_at,rowid", (r['id'], r['preference_version']))]
        applied = [dict(d) for d in c.execute('SELECT title,outcome,state,correction,review_json FROM morning_decisions WHERE run_id=? AND preference_version=? ORDER BY created_at', (r['id'], r['preference_version']))]
        report_context = {'events': evidence, 'applied_decisions': applied}
        basis = hashlib.sha256(dump(report_context).encode()).hexdigest()[:24]
        status = c.execute('SELECT * FROM morning_completion WHERE run_id=?', (r['id'],)).fetchone()
        if not status or status['report_basis'] != basis:
            # If an earlier report is still in flight, retain it; a newer report will
            # include the intervening review and follow-up rather than dropping them.
            tid = self.queue(c, r, 'grok', 'final', lambda t: self.final_prompt(c, r, t, evidence, applied), f'final:{basis}')
            c.execute('INSERT INTO morning_completion(run_id,report_basis,report_task_id) VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET report_basis=excluded.report_basis,report_task_id=excluded.report_task_id', (r['id'], basis, tid))
            c.execute("UPDATE morning_runs SET phase='reporting' WHERE id=?", (r['id'],))
            return
        report = c.execute("SELECT 1 FROM morning_events e JOIN morning_steps s ON s.task_id=e.task_id WHERE (s.task_id=? OR s.repair_for=?) AND e.category='final' AND e.processing='valid'", (status['report_task_id'], status['report_task_id'])).fetchone()
        if report and not self.obligations(c, r):
            deferred = self.deferred(c, r)
            self.stop(c, r, 'Automatic work is complete. A choice still needs you.' if deferred else 'Automatic research is complete. The findings are ready to review.', 'needs_user' if deferred else 'completed')

    def final_prompt(self, c, r, t, evidence, applied):
        lines = []
        for d in applied:
            review = json.loads(d['review_json']) if d['review_json'] else {}
            lines.append(f"- {d['title']}: {d['outcome'] or 'not reviewed'} ({d['state']})"
                         + ('; held by boundary check' if d['correction'] else '')
                         + (f"; Grok: {prompts.clip(review.get('explanation'), 220)}" if review else ''))
        results = [e for e in evidence if e['sender'] == 'instinct']
        latest = next((e for e in reversed(results) if e['category'] == 'final'), results[-1] if results else None)
        data = json.loads(latest['content_json']) if latest and latest['content_json'] else {}
        result = (latest['text'] + ' ' + prompts.evidence(data)) if latest else 'No Instinct result.'
        unknowns = list(dict.fromkeys(u for e in results for u in json.loads(e['content_json'] or '{}').get('unknowns', [])))
        said = [e['text'] for e in evidence if e['sender'] == 'you' and e['category'] == 'instruction']
        return prompts.grok_final(t, r['preference_version'], lines, result, ' | '.join(unknowns) or 'none reported',
                                  ' | '.join(said) or 'none', self.boundary(c, r))

    def repair_prompt(self, c, r, agent, original_task, error, original):
        """One formatting repair. Grok receives the essentials again within its preview budget."""
        task = c.execute('SELECT * FROM tasks WHERE id=?', (original_task,)).fetchone()
        if agent != 'grok':
            return render('repair', error=error, original=original, instruction=task['instructions'])
        step = c.execute('SELECT * FROM morning_steps WHERE task_id=?', (original_task,)).fetchone()
        category = {'review': 'review', 'final': 'final'}.get(step['kind'], 'acknowledgement')
        essentials = ''
        if step['kind'] == 'review':
            d = c.execute('SELECT * FROM morning_decisions WHERE id=?', (step['link'],)).fetchone()
            essentials = prompts.decision_block(json.loads(d['proposal_json']), json.loads(d['checks_json']))
        brief = self.boundary(c, r)
        return lambda t: prompts.grok_repair(t, r['preference_version'], original_task, error, category,
                                             essentials, original, brief, step['link'])

    def advance(self):
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute("SELECT * FROM morning_runs WHERE phase IN ('onboarding','running','reporting') AND demo=0").fetchall():
                r = dict(row)
                if r['phase'] != 'onboarding' and r['end_at'] and r['end_at'] <= now():
                    self.stop(c, r, 'The overnight window ended. Received results are saved; late replies remain in history.')
                    continue
                rejected = c.execute('''SELECT i.*,t.instructions FROM inbound_receipts i JOIN tasks t ON t.id=i.task_id
                    WHERE i.error IS NOT NULL AND t.request_id=? AND t.constraint_version=? AND t.phase!='gm_repair'
                    AND NOT EXISTS (SELECT 1 FROM morning_events e JOIN morning_steps s ON s.task_id=e.task_id
                        WHERE (s.task_id=t.id OR s.repair_for=t.id) AND e.processing='valid' AND e.sender!='you'
                        AND e.category NOT IN ('acknowledgement','error'))''',
                    (r['id'], r['preference_version'])).fetchall()
                for receipt in rejected:
                    step = c.execute('SELECT * FROM morning_steps WHERE task_id=?', (receipt['task_id'],)).fetchone()
                    self.queue(c, r, receipt['agent'], 'repair', self.repair_prompt(c, r, receipt['agent'], receipt['task_id'],
                        receipt['error'], receipt['content']), f'repair:{receipt["task_id"]}',
                        link=step['link'], repair_for=receipt['task_id'])
                events = c.execute('SELECT * FROM morning_events WHERE run_id=? AND handled=0 ORDER BY created_at,rowid', (r['id'],)).fetchall()
                for e in events:
                    c.execute('UPDATE morning_events SET handled=1 WHERE id=?', (e['id'],))
                    if e['preference_version'] != r['preference_version'] or e['processing'] in ('stale','late'):
                        continue
                    step = c.execute('SELECT * FROM morning_steps WHERE task_id=?', (e['task_id'],)).fetchone()
                    if e['processing'] == 'malformed':
                        recovered = c.execute('''SELECT 1 FROM morning_events e JOIN morning_steps s ON s.task_id=e.task_id
                            WHERE (s.task_id=? OR s.repair_for=?) AND e.processing='valid' AND e.sender!='you'
                            AND e.category NOT IN ('acknowledgement','error')''', (e['task_id'], e['task_id'])).fetchone()
                        if recovered:
                            continue
                        if step['kind'] != 'repair':
                            self.queue(c, r, e['sender'], 'repair', self.repair_prompt(c, r, e['sender'], e['task_id'], e['error'], e['text']),
                                       f'repair:{e["task_id"]}', link=step['link'], repair_for=e['task_id'])
                        continue
                    if e['category'] in ('acknowledgement', 'error', 'onboarding') or not e['content_json']:
                        continue
                    if r['phase'] == 'onboarding':
                        continue
                    data = json.loads(e['content_json'])
                    if e['sender'] == 'grok':
                        if e['category'] == 'review':
                            self.apply_review(c, r, e, data)
                        continue
                    if self.review_decision(c, r, e, data):
                        if self.get(c, r['id'])['phase'] == 'finished':
                            break
                        continue
                # Context forwarding is bounded and batched; each event is included once in an outgoing intent.
                if self.get(c, r['id'])['phase'] in ('running','reporting'):
                    context = self.context(c, r)
                    if context:
                        key = hashlib.sha256(dump([e['id'] for e in context]).encode()).hexdigest()[:20]
                        brief = self.boundary(c, r)
                        self.queue(c, r, 'grok', 'context', lambda t, r=r, context=context, brief=brief: prompts.grok_context(t, r['preference_version'], context, brief),
                                   f'context:{key}', context_ids=[e['id'] for e in context])
                c.execute("UPDATE morning_decisions SET state='resolved' WHERE state='pending' AND outcome IN ('approve','deny','redirect') AND followup_id IN (SELECT id FROM tasks WHERE delivery='sent')")
                self.keep_going(c, self.get(c, r['id']))
                self.settle(c, self.get(c, r['id']))
            c.execute("UPDATE morning_decisions SET state='resolved' WHERE state='pending' AND outcome IN ('approve','deny','redirect') AND followup_id IN (SELECT id FROM tasks WHERE delivery='sent')")

    def snapshot(self):
        with self.store.db() as c:
            result = []
            for row in c.execute('SELECT * FROM morning_runs WHERE archived=0 ORDER BY created_at DESC').fetchall():
                r = dict(row)
                r['preferences'] = self.prefs(c, r)
                setup = c.execute('SELECT * FROM morning_setup WHERE run_id=?', (r['id'],)).fetchone()
                r['setup'] = {'step': setup['step'], 'answers': json.loads(setup['answers_json'])} if setup else None
                r['verification'] = bool(setup and setup['verification'])
                done = c.execute('SELECT finish_kind FROM morning_completion WHERE run_id=?', (r['id'],)).fetchone()
                pending = self.obligations(c, r) if r['phase'] in ('running','reporting') else []
                r['pending_task_ids'] = [t['id'] for t in pending]
                r['workflow_status'] = (done[0] if done and done[0] else 'stopped') if r['phase']=='finished' else (
                    'failed' if any(t['delivery'] in ('failed','uncertain') or t['work']=='failed' for t in pending) else
                    'waiting_review' if any(t['kind']=='review' for t in pending) else
                    'waiting_followup' if any(t['kind'] in ('relay','manual') for t in pending) else
                    'waiting_delivery' if any(t['delivery']!='sent' for t in pending) else r['phase'])
                r['events'] = []
                for event in c.execute('''SELECT e.*,m.observed_at FROM morning_events e LEFT JOIN messages m ON m.id=e.message_id
                                         WHERE e.run_id=? ORDER BY e.created_at,e.rowid''', (r['id'],)):
                    e = dict(event)
                    e['content'] = json.loads(e.pop('content_json')) if e['content_json'] else None
                    e['display'] = json.loads(e.pop('display_json')) if e.get('display_json') else None
                    r['events'].append(e)
                r['decisions'] = []
                for dec in c.execute('SELECT * FROM morning_decisions WHERE run_id=? ORDER BY created_at', (r['id'],)):
                    d = dict(dec)
                    for name in ('proposal', 'checks', 'review', 'display'):
                        d[name] = json.loads(d.pop(name + '_json')) if d.get(name + '_json') else None
                    instruction = c.execute('SELECT text FROM morning_instructions WHERE task_id=?', (d['followup_id'],)).fetchone()
                    d['instruction'] = instruction[0] if instruction else None
                    family = {t[0] for t in c.execute('SELECT task_id FROM morning_steps WHERE task_id=? OR repair_for=?', (d['followup_id'], d['followup_id']))}
                    d['response_event_ids'] = [e['id'] for e in r['events'] if e['task_id'] in family and e['sender']=='instinct']
                    r['decisions'].append(d)
                r['tasks'] = [dict(t) for t in c.execute('''SELECT id,agent,phase,delivery,work,error,warning,created_at,sent_at,
                             acknowledged_at,completed_at,constraint_version FROM tasks WHERE request_id=? ORDER BY created_at''', (r['id'],))]
                findings = [e for e in r['events'] if e['sender']=='instinct' and e['processing']=='valid' and e['preference_version']==r['preference_version'] and e['category'] in ('context','decision_required','final')]
                r['current_result_id'] = findings[-1]['id'] if findings else None
                r['demo'] = bool(r['demo'])
                stops = [e['created_at'] for e in r['events'] if e['category'] == 'stop']
                r['completed_at'] = stops[-1] if r['phase'] == 'finished' and stops else None
                result.append(r)
            health = [dict(h) for h in c.execute('SELECT * FROM health')]
            unassigned = [dict(m) for m in c.execute('SELECT id,agent,content,reason,received_at FROM unassigned_messages WHERE assigned_task_id IS NULL')]
            unassigned += [dict(m) for m in c.execute("SELECT id,agent,content,error AS reason,received_at FROM inbound_receipts WHERE task_id IS NULL AND error IS NOT NULL")]
            rejected = [dict(m) for m in c.execute('''SELECT i.id,i.content,i.error,i.received_at,t.request_id FROM inbound_receipts i
                         JOIN tasks t ON t.id=i.task_id WHERE i.error IS NOT NULL AND t.origin='good_morning' ''')]
            return {'runs': result, 'health': health, 'unassigned': unassigned, 'rejected': rejected}
