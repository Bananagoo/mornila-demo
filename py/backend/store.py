import hashlib
import json
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL

from backend.models import CONSTRAINTS


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return str(uuid4())


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, root_id TEXT NOT NULL, parent_id TEXT, agent TEXT NOT NULL,
              instructions TEXT NOT NULL, constraints_json TEXT NOT NULL, constraint_version INTEGER NOT NULL,
              revision INTEGER NOT NULL, purpose TEXT NOT NULL, created_at TEXT NOT NULL,
              delivery TEXT NOT NULL DEFAULT 'queued', work TEXT NOT NULL DEFAULT 'queued',
              sent_at TEXT, acknowledged_at TEXT, completed_at TEXT, error TEXT, warning TEXT,
              provider_message_id TEXT, provider_thread_id TEXT, rfc_message_id TEXT NOT NULL,
              latest_message_id TEXT, UNIQUE(root_id, revision));
            CREATE TABLE IF NOT EXISTS submissions (
              key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, started_at TEXT NOT NULL,
              finished_at TEXT, status TEXT NOT NULL, error TEXT, metadata_json TEXT);
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, agent TEXT NOT NULL,
              provider_message_id TEXT NOT NULL, provider_thread_id TEXT, rfc_message_id TEXT,
              direction TEXT NOT NULL, received_at TEXT NOT NULL, observed_at TEXT,
              content TEXT NOT NULL, raw_json TEXT NOT NULL, update_json TEXT,
              extraction_error TEXT, stale INTEGER NOT NULL DEFAULT 0,
              UNIQUE(agent, provider_message_id, direction));
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, proposal_id TEXT,
              action TEXT NOT NULL, note TEXT NOT NULL, scope TEXT NOT NULL,
              followup_id TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health (
              name TEXT PRIMARY KEY, checked_at TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS poll_state (
              name TEXT PRIMARY KEY, started_at REAL NOT NULL, completed_until REAL NOT NULL,
              window_start REAL, window_end REAL, page_token TEXT);
            CREATE TABLE IF NOT EXISTS unassigned_messages (
              id TEXT PRIMARY KEY, agent TEXT NOT NULL, provider_message_id TEXT NOT NULL UNIQUE,
              provider_thread_id TEXT, rfc_message_id TEXT, received_at TEXT NOT NULL,
              observed_at TEXT, content TEXT NOT NULL, raw_json TEXT NOT NULL, reason TEXT NOT NULL,
              assigned_task_id TEXT);
            CREATE TABLE IF NOT EXISTS observations (
              id TEXT PRIMARY KEY, message_id TEXT NOT NULL, kind TEXT NOT NULL,
              value_json TEXT NOT NULL, authority TEXT NOT NULL DEFAULT 'agent_claim');
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, submission_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
              started_at TEXT NOT NULL, deadline_at TEXT NOT NULL, state TEXT NOT NULL,
              max_rounds INTEGER NOT NULL DEFAULT 2);
            CREATE TABLE IF NOT EXISTS review_steps (
              run_id TEXT NOT NULL, agent TEXT NOT NULL, stage TEXT NOT NULL,
              task_id TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
              PRIMARY KEY (run_id, agent, stage));
            """)
            # Additive migration: existing evidence and task identifiers are never replaced.
            for table, columns in {
                "tasks": {"group_id": "TEXT", "run_id": "TEXT", "origin": "TEXT NOT NULL DEFAULT 'owner'"},
                "decisions": {"origin": "TEXT NOT NULL DEFAULT 'owner'"},
            }.items():
                present = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in present:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            c.execute("UPDATE tasks SET group_id=root_id WHERE group_id IS NULL")
            from backend.supervision import migrate

            migrate(c)
            from backend.morning import migrate as migrate_morning

            migrate_morning(c)
        path.chmod(0o600)

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, sql, args=()):
        with self.db() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def task(self, task_id):
        rows = self.all("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise KeyError(task_id)
        return rows[0]

    def create(self, key, data, parent_id=None, connection=None):
        fingerprint = hashlib.sha256(json.dumps([parent_id, data], sort_keys=True).encode()).hexdigest()
        with self.db() if connection is None else nullcontext(connection) as c:
            if connection is None:
                c.execute("BEGIN IMMEDIATE")
            old = c.execute("SELECT * FROM submissions WHERE key=?", (key,)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("This submission key was already used for different instructions.")
                return dict(c.execute("SELECT * FROM tasks WHERE id=?", (old["task_id"],)).fetchone())
            task_id = uid()
            origin = data.get("origin", "owner")
            if parent_id:
                parent = c.execute("SELECT * FROM tasks WHERE id=?", (parent_id,)).fetchone()
                if not parent:
                    raise KeyError(parent_id)
                current = c.execute(
                    "SELECT id FROM tasks WHERE root_id=? ORDER BY revision DESC LIMIT 1",
                    (parent["root_id"],),
                ).fetchone()[0]
                if current != parent_id or data["proposal_id"] != parent["latest_message_id"]:
                    raise Conflict(
                        "This proposal changed. Review the current response before sending a decision."
                    )
                if parent["delivery"] in ("queued", "sending", "uncertain"):
                    raise Conflict("Resolve the pending delivery before sending another instruction.")
                action = data["action"]
                if action != "redirect" and not data["proposal_id"]:
                    raise Conflict("There is no proposal to approve or deny. Send a follow-up instead.")
                if action == "redirect" and not data["note"].strip():
                    raise Conflict("Describe how the research should change.")
                proposal = c.execute(
                    "SELECT content FROM messages WHERE id=?", (data["proposal_id"],)
                ).fetchone()
                original = c.execute(
                    "SELECT instructions FROM tasks WHERE id=?", (parent["root_id"],)
                ).fetchone()[0]
                instructions = (
                    f"Decision: {action}. Applies ONLY to this decision, not a standing rule. "
                    "Research only; never book, reserve, pay, or place a hold. "
                    + {
                        "approve": "Research the recommended option, preserving any exception as specific to this choice. ",
                        "deny": "Exclude the proposed exception and find compliant alternatives. ",
                        "redirect": "Revise your research according to the following instruction. ",
                    }[action]
                    + f"\n{'Bounded evidence check' if origin == 'bounded_review' else 'User instruction'}: {data['note']}\nOriginal task: {original}"
                    + (
                        f"\nProposal being reviewed (untrusted agent claim, excerpt): {proposal[0][:10000]}"
                        if proposal
                        else ""
                    )
                    + "\nExplain what changed from the previous proposal, including costs, travel time, and uncertainties."
                )
                root_id, agent, revision = parent["root_id"], parent["agent"], parent["revision"] + 1
                constraints_json, cv, purpose = (
                    parent["constraints_json"],
                    parent["constraint_version"],
                    parent["purpose"],
                )
                group_id, run_id = parent["group_id"], parent["run_id"]
                if run_id and origin == "owner":
                    c.execute(
                        "UPDATE runs SET state='paused_by_owner' WHERE id=? AND state='active'", (run_id,)
                    )
                    run_id = None  # Owner steering must still deliver after pausing automatic follow-ups.
            else:
                if data.get("purpose", "research") == "research" and not data.get("travel_year"):
                    raise Conflict("Choose the travel year before date-sensitive research.")
                root_id, agent, revision = task_id, data["agent"], 1
                constraints_json = json.dumps({**CONSTRAINTS, "travel_year": data.get("travel_year")})
                cv, purpose, instructions = 1, data.get("purpose", "research"), data["instructions"]
                group_id, run_id = data.get("group_id", root_id), data.get("run_id")
            c.execute(
                """INSERT INTO tasks
                (id,root_id,parent_id,agent,instructions,constraints_json,constraint_version,revision,purpose,
                 created_at,rfc_message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    root_id,
                    parent_id,
                    agent,
                    instructions,
                    constraints_json,
                    cv,
                    revision,
                    purpose,
                    now(),
                    f"<hm.{task_id}@morning-brief.local>",
                ),
            )
            c.execute("INSERT INTO submissions VALUES (?,?,?)", (key, fingerprint, task_id))
            c.execute(
                "UPDATE tasks SET group_id=?,run_id=?,origin=? WHERE id=?",
                (group_id, run_id, origin, task_id),
            )
            # Durable outgoing intent exists before any provider operation (including the first attempt).
            c.execute(
                """INSERT INTO messages (id,task_id,agent,provider_message_id,rfc_message_id,
                direction,received_at,content,raw_json) VALUES (?,?,?,?,?,'out',?,?,?)""",
                (
                    uid(),
                    task_id,
                    agent,
                    task_id,
                    f"<hm.{task_id}@morning-brief.local>",
                    now(),
                    instructions,
                    json.dumps({"instruction": instructions, "agent": agent, "constraint_version": cv}),
                ),
            )
            if parent_id:
                c.execute(
                    "INSERT INTO decisions (id,task_id,proposal_id,action,note,scope,followup_id,created_at,origin) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        uid(),
                        parent_id,
                        data["proposal_id"],
                        data["action"],
                        data["note"],
                        data["scope"],
                        task_id,
                        now(),
                        origin,
                    ),
                )
            return dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def submit(self, key, data):
        agents = ("instinct", "grok") if data["agent"] == "both" else (data["agent"],)
        group_id = str(uuid5(NAMESPACE_URL, "morning-brief:" + key))
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            previous = c.execute("SELECT fingerprint FROM submissions WHERE key=?", (key,)).fetchone()
            if previous and previous[0] != fingerprint:
                raise Conflict("This submission key was already used for different instructions.")
            tasks = [
                self.create(key + ":" + agent, {**data, "agent": agent, "group_id": group_id}, connection=c)
                for agent in agents
            ]
            c.execute("INSERT OR IGNORE INTO submissions VALUES (?,?,?)", (key, fingerprint, tasks[0]["id"]))
            return tasks

    def followup_group(self, group_id, key, data):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            result, agents = [], set()
            fingerprint = hashlib.sha256(json.dumps([group_id, data], sort_keys=True).encode()).hexdigest()
            old = c.execute("SELECT fingerprint FROM submissions WHERE key=?", (key,)).fetchone()
            if old and old[0] != fingerprint:
                raise Conflict("This submission key was already used for different instructions.")
            for target in data["targets"]:
                parent = c.execute(
                    "SELECT * FROM tasks WHERE id=? AND group_id=?", (target["task_id"], group_id)
                ).fetchone()
                if not parent or parent["agent"] in agents:
                    raise Conflict("Choose one current conversation per recipient agent.")
                agents.add(parent["agent"])
                result.append(
                    self.create(
                        key + ":" + parent["agent"],
                        {
                            "action": "redirect",
                            "note": data["note"],
                            "scope": "this_decision",
                            "proposal_id": target["proposal_id"],
                        },
                        parent["id"],
                        connection=c,
                    )
                )
            c.execute("INSERT OR IGNORE INTO submissions VALUES (?,?,?)", (key, fingerprint, result[0]["id"]))
            return result

    def assign(self, message_id, task_id):
        from backend.adapters.common import extract

        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            message = c.execute("SELECT * FROM unassigned_messages WHERE id=?", (message_id,)).fetchone()
            if not message:
                raise KeyError(message_id)
            if message["assigned_task_id"] and message["assigned_task_id"] != task_id:
                raise Conflict("This reply was already assigned to another task.")
            update, error = extract(message["content"], task_id)
            result = self.ingest(
                task_id,
                "instinct",
                message["provider_message_id"],
                message["content"],
                json.loads(message["raw_json"]),
                update,
                error,
                message["provider_thread_id"],
                message["rfc_message_id"],
                message["observed_at"],
                connection=c,
            )
            c.execute("UPDATE messages SET received_at=? WHERE id=?", (message["received_at"], result[0]))
            c.execute("UPDATE unassigned_messages SET assigned_task_id=? WHERE id=?", (task_id, message_id))
            return result

    def prepare_outgoing(self, task, content, raw):
        with self.db() as c:
            c.execute(
                "UPDATE messages SET content=?,raw_json=? WHERE task_id=? AND direction='out'",
                (content, json.dumps(raw), task["id"]),
            )

    def seen_incoming(self, provider_id):
        return bool(
            self.all(
                "SELECT id FROM messages WHERE agent='instinct' AND provider_message_id=? AND direction='in' "
                "UNION SELECT id FROM unassigned_messages WHERE provider_message_id=?",
                (provider_id, provider_id),
            )
        )

    def unassigned(self, message, text, observed_at, reason):
        from backend.adapters.gmail import headers

        h = headers(message)
        with self.db() as c:
            c.execute(
                "INSERT OR IGNORE INTO unassigned_messages VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    uid(),
                    "instinct",
                    message["id"],
                    message.get("threadId"),
                    h.get("message-id"),
                    now(),
                    observed_at,
                    text,
                    json.dumps(message),
                    reason,
                ),
            )

    def claim(self, task_id=None):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "UPDATE tasks SET delivery='failed',error='Research window ended or paused before delivery; not sent.' "
                "WHERE delivery='queued' AND run_id IS NOT NULL AND run_id IN "
                "(SELECT id FROM runs WHERE deadline_at<=? OR state!='active')",
                (now(),),
            )
            c.execute(
                "UPDATE tasks SET delivery='superseded',warning='Request changed or its research window ended before dispatch.' WHERE delivery='queued' AND request_id IN (SELECT id FROM requests WHERE managed=1) AND EXISTS (SELECT 1 FROM requests r WHERE r.id=tasks.request_id AND (r.revision!=tasks.request_revision OR r.preference_version!=tasks.constraint_version OR r.deadline_at<=?))",
                (now(),),
            )
            c.execute(
                "UPDATE tasks SET delivery='superseded',warning='Brief changed or overnight window closed before delivery.' "
                "WHERE delivery='queued' AND origin='good_morning' AND EXISTS "
                "(SELECT 1 FROM morning_runs r WHERE r.id=tasks.request_id AND "
                "(r.preference_version!=tasks.constraint_version OR r.phase='finished' OR "
                "(r.phase!='onboarding' AND r.end_at<=?)))", (now(),),
            )
            t = c.execute(
                "SELECT * FROM tasks t WHERE delivery='queued' AND (? IS NULL OR id=?) AND NOT EXISTS (SELECT 1 FROM tasks older WHERE older.request_id=t.request_id AND older.agent=t.agent AND older.id!=t.id AND older.delivery IN ('sending','uncertain')) AND NOT EXISTS (SELECT 1 FROM tasks prior WHERE t.origin='good_morning' AND prior.request_id=t.request_id AND prior.agent=t.agent AND prior.constraint_version=t.constraint_version AND prior.revision<t.revision AND prior.delivery IN ('queued','sending','failed','uncertain')) ORDER BY created_at LIMIT 1",
                (task_id, task_id),
            ).fetchone()
            if not t:
                return None
            attempt = uid()
            c.execute("UPDATE tasks SET delivery='sending',error=NULL WHERE id=?", (t["id"],))
            c.execute(
                "INSERT INTO attempts (id,task_id,started_at,status) VALUES (?,?,?,'sending')",
                (attempt, t["id"], now()),
            )
            return dict(t), attempt

    def delivery(self, task_id, attempt, status, metadata=None, error=None):
        metadata = metadata or {}
        with self.db() as c:
            c.execute(
                "UPDATE attempts SET finished_at=?,status=?,error=?,metadata_json=? WHERE id=?",
                (now(), status, error, json.dumps(metadata), attempt),
            )
            if status == "sent":
                c.execute(
                    "UPDATE messages SET provider_message_id=COALESCE(?,provider_message_id),provider_thread_id=? "
                    "WHERE task_id=? AND direction='out'",
                    (metadata.get("message_id"), metadata.get("thread_id"), task_id),
                )
            c.execute(
                """UPDATE tasks SET delivery=?,error=?,
                work=CASE WHEN work='queued' THEN ? ELSE work END,
                sent_at=COALESCE(sent_at,?),provider_message_id=COALESCE(?,provider_message_id),
                provider_thread_id=COALESCE(?,provider_thread_id) WHERE id=?""",
                (
                    status,
                    error,
                    "waiting" if status == "sent" else "queued",
                    now() if status == "sent" else None,
                    metadata.get("message_id"),
                    metadata.get("thread_id"),
                    task_id,
                ),
            )

    def recover_interrupted(self):
        with self.db() as c:
            error = "Worker stopped during delivery. Check provider history before any resend."
            c.execute("UPDATE tasks SET delivery='uncertain',error=? WHERE delivery='sending'", (error,))
            c.execute(
                "UPDATE attempts SET status='uncertain',finished_at=?,error=? WHERE status='sending'",
                (now(), error),
            )

    def retry(self, task_id):
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not t:
                raise KeyError(task_id)
            current = c.execute(
                "SELECT id FROM tasks WHERE root_id=? ORDER BY revision DESC LIMIT 1", (t["root_id"],)
            ).fetchone()[0]
            if (current != task_id and t["origin"] != "good_morning") or t["delivery"] != "failed":
                raise Conflict("Only a confirmed failed delivery of the current instruction can be retried.")
            if t["request_id"]:
                r = c.execute("SELECT * FROM requests WHERE id=?", (t["request_id"],)).fetchone()
                attempts = c.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (task_id,)).fetchone()[
                    0
                ]
                if (
                    r
                    and r["managed"]
                    and (r["revision"] != t["request_revision"] or r["deadline_at"] <= now() or attempts >= 3)
                ):
                    raise Conflict(
                        "This instruction is out of date, outside its window, or has reached three delivery attempts. Send a new instruction after resolving the cause."
                    )
            if t["origin"] == "good_morning":
                r = c.execute("SELECT * FROM morning_runs WHERE id=?", (t["request_id"],)).fetchone()
                if r["preference_version"] != t["constraint_version"] or r["phase"] == "finished" or (
                    r["phase"] != "onboarding" and r["end_at"] <= now()
                ):
                    raise Conflict("This overnight instruction is out of date or its window ended.")
            c.execute("UPDATE tasks SET delivery='queued',error=NULL WHERE id=?", (task_id,))

    def ingest(
        self,
        task_id,
        agent,
        provider_id,
        content,
        raw,
        update=None,
        extraction_error=None,
        thread_id=None,
        rfc_id=None,
        observed_at=None,
        ambiguous=False,
        connection=None,
    ):
        with self.db() if connection is None else nullcontext(connection) as c:
            if connection is None:
                c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM messages WHERE agent=? AND provider_message_id=? AND direction='in'",
                (agent, provider_id),
            ).fetchone()
            raw_json = json.dumps(raw, sort_keys=True)
            if old:
                if agent == "grok" and (old["task_id"] != task_id or old["raw_json"] != raw_json):
                    raise Conflict("Event ID already used with different content.")
                return old["id"], True
            t = c.execute("SELECT * FROM tasks WHERE id=? AND agent=?", (task_id, agent)).fetchone()
            if not t:
                raise KeyError(task_id)
            if t["delivery"] in ("queued", "failed", "superseded"):
                raise Conflict("No sent instruction exists for this response.")
            current = c.execute(
                "SELECT id FROM tasks WHERE root_id=? ORDER BY revision DESC LIMIT 1", (t["root_id"],)
            ).fetchone()[0]
            latest = c.execute(
                "SELECT observed_at FROM messages WHERE id=?", (t["latest_message_id"],)
            ).fetchone()
            stale = (
                ambiguous
                or current != task_id
                or bool(latest and observed_at and latest[0] and observed_at < latest[0])
            )
            if t["request_id"]:
                r = c.execute("SELECT * FROM requests WHERE id=?", (t["request_id"],)).fetchone()
                stale = stale or bool(
                    r
                    and r["managed"]
                    and (
                        t["request_revision"] != r["revision"]
                        or t["constraint_version"] != r["preference_version"]
                    )
                )
            if t["origin"] == "good_morning":
                r = c.execute("SELECT * FROM morning_runs WHERE id=?", (t["request_id"],)).fetchone()
                stale = ambiguous or t["constraint_version"] != r["preference_version"] or bool(
                    latest and observed_at and latest[0] and observed_at < latest[0]
                )
            message_id = uid()
            c.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    task_id,
                    agent,
                    provider_id,
                    thread_id,
                    rfc_id,
                    "in",
                    now(),
                    observed_at,
                    content,
                    raw_json,
                    json.dumps(update) if update else None,
                    extraction_error,
                    int(stale),
                ),
            )
            if not stale:
                kind = update["update_type"] if update else "message"
                work = {"result": "completed", "error": "failed", "acknowledgement": "acknowledged"}.get(
                    kind, "waiting"
                )
                # A late acknowledgement/progress must not regress a completed result.
                if t["work"] == "completed" and kind in ("acknowledgement", "progress"):
                    c.execute("UPDATE messages SET stale=1 WHERE id=?", (message_id,))
                else:
                    c.execute(
                        """UPDATE tasks SET latest_message_id=?,work=?,warning=NULL,
                        acknowledged_at=CASE WHEN ?='acknowledgement' THEN ? ELSE acknowledged_at END,
                        completed_at=CASE WHEN ?='result' THEN ? ELSE completed_at END WHERE id=?""",
                        (message_id, work, kind, now(), kind, now(), task_id),
                    )
            if update:
                for field, kind in (
                    ("claims", "claim"),
                    ("sources", "source"),
                    ("missing_evidence", "missing_evidence"),
                    ("disagreements", "disagreement"),
                    ("corrections", "correction"),
                ):
                    for value in update.get(field, []):
                        c.execute(
                            "INSERT INTO observations VALUES (?,?,?,?,?)",
                            (uid(), message_id, kind, json.dumps(value), "agent_claim"),
                        )
                for kind, value in (
                    (update["update_type"], update["content"]),
                    ("decision", update.get("requested_decision")),
                ):
                    if value:
                        c.execute(
                            "INSERT INTO observations VALUES (?,?,?,?,?)",
                            (uid(), message_id, kind, json.dumps(value), "agent_claim"),
                        )
            from backend.supervision import Supervisor

            if t["origin"] == "good_morning":
                from backend.morning import Morning
                Morning(self).receive(c, dict(t), message_id, update, extraction_error)
            else:
                Supervisor(self).process_message(c, dict(t), message_id, update, extraction_error)
            return message_id, False

    def health(self, name, error=None):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO health VALUES (?,?,?)", (name, now(), error))

    def snapshot(self):
        from backend.supervision import Supervisor

        requests = Supervisor(self).snapshot()
        tasks = self.all("SELECT * FROM tasks ORDER BY created_at DESC")
        messages = self.all("SELECT * FROM messages WHERE direction='in' ORDER BY received_at DESC")
        for m in messages:
            m["update"] = json.loads(m.pop("update_json")) if m["update_json"] else None
            m.pop("raw_json")  # Original text is exposed; transport payload is private, on disk.
        for t in tasks:
            t["constraints"] = json.loads(t.pop("constraints_json"))
        unassigned = self.all(
            "SELECT * FROM unassigned_messages WHERE assigned_task_id IS NULL ORDER BY received_at DESC"
        )
        for m in unassigned:
            m.pop("raw_json")
        return {
            "requests": requests,
            "preference_defaults": Supervisor(self).defaults(),
            "tasks": tasks,
            "messages": messages,
            "decisions": self.all("SELECT * FROM decisions ORDER BY created_at DESC"),
            "health": self.all("SELECT * FROM health"),
            "attempts": self.all("SELECT * FROM attempts ORDER BY started_at DESC"),
            "outgoing": self.all(
                "SELECT task_id,agent,received_at,content FROM messages WHERE direction='out' ORDER BY received_at DESC"
            ),
            "unassigned": unassigned,
            "runs": self.all("SELECT * FROM runs ORDER BY started_at DESC"),
            "review_steps": self.all("SELECT * FROM review_steps"),
            "observations": self.all("SELECT * FROM observations"),
        }
