"""Persistent draft cards and jobs for Demo 1; no fabricated legal entity records."""

import hashlib
import json
import logging
from contextlib import closing
from uuid import uuid4

from .llm_store import LLMRepository, stamp
from .schema import connect, execute_statements
from parser.public_http import website_key
from app.services.llm.card_chat import refresh_reply
from app.services.llm.card_edits import edited_fact
from app.services.llm.changes import effective_facts, normalized
from app.services.llm.workflow_models import WorkflowError

LOGGER = logging.getLogger(__name__)


class DemoRepository(LLMRepository):
    def _previous_run(self, db, institution):
        row = db.execute("SELECT latest_run FROM llm_card WHERE id=?", (institution.id,)).fetchone()
        if row is None:
            return super()._previous_run(db, institution)
        previous = self._read(db, row[0]) if row[0] else None
        return previous if previous and previous.institution == institution else None

    def _after_create(self, db, run):
        # Capture approved baseline and switch latest in the same transaction.
        db.execute("UPDATE llm_card SET latest_run=? WHERE id=?", (run.run_id, run.institution.id))

    def __init__(self, path):
        super().__init__(path)
        with closing(connect(self.path)) as db, db:
            execute_statements(db, '''
            CREATE TABLE IF NOT EXISTS llm_card (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, website TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1, latest_run TEXT REFERENCES llm_run(run_id),
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS llm_job (
              id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES llm_card(id),
              request_key TEXT NOT NULL, task_type TEXT NOT NULL,
              payload TEXT NOT NULL CHECK(json_valid(payload)), UNIQUE(card_id,request_key));
            CREATE TABLE IF NOT EXISTS llm_saved_material (
              material_id TEXT PRIMARY KEY REFERENCES llm_material(material_id), saved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS llm_report_migration (
              key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS llm_purge_context (
              card_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS llm_chat_turn (
              id INTEGER PRIMARY KEY,
              card_id TEXT NOT NULL REFERENCES llm_card(id),
              user_id TEXT NOT NULL,
              run_id TEXT REFERENCES llm_run(run_id),
              revision INTEGER,
              question TEXT NOT NULL,
              answer TEXT NOT NULL CHECK(json_valid(answer)));
            CREATE INDEX IF NOT EXISTS llm_chat_history ON llm_chat_turn(user_id, card_id, id);
            CREATE TABLE IF NOT EXISTS llm_general_chat_turn (
              id INTEGER PRIMARY KEY,
              user_id TEXT NOT NULL,
              question TEXT NOT NULL,
              answer TEXT NOT NULL CHECK(json_valid(answer)));
            CREATE INDEX IF NOT EXISTS llm_general_chat_history ON llm_general_chat_turn(user_id, id);
            ''')
            execute_statements(db, '''
            DROP TRIGGER IF EXISTS llm_review_no_delete;
            CREATE TRIGGER llm_review_no_delete BEFORE DELETE ON llm_review
              WHEN NOT EXISTS (
                SELECT 1 FROM llm_purge_context p JOIN llm_run r ON r.institution_id=p.card_id
                WHERE r.run_id=OLD.run_id
              ) BEGIN SELECT RAISE(ABORT,'LLM review audit is immutable'); END;
            DROP TRIGGER IF EXISTS llm_input_no_delete;
            CREATE TRIGGER llm_input_no_delete BEFORE DELETE ON llm_run_input
              WHEN NOT EXISTS (
                SELECT 1 FROM llm_purge_context p JOIN llm_run r ON r.institution_id=p.card_id
                WHERE r.run_id=OLD.run_id
              ) BEGIN SELECT RAISE(ABORT,'LLM source snapshot is immutable'); END;
            DROP TRIGGER IF EXISTS llm_material_fact_no_delete;
            CREATE TRIGGER llm_material_fact_no_delete BEFORE DELETE ON llm_material_fact
              WHEN NOT EXISTS (
                SELECT 1 FROM llm_purge_context p
                JOIN llm_material m ON m.material_id=OLD.material_id
                JOIN llm_run r ON r.run_id=m.run_id
                WHERE r.institution_id=p.card_id
              ) BEGIN SELECT RAISE(ABORT,'Material fact snapshot is immutable'); END;
            ''')
            # Keep reports that existed before explicit saving was introduced.
            if not db.execute("SELECT 1 FROM llm_report_migration WHERE key='legacy_reports' ").fetchone():
                db.execute("INSERT OR IGNORE INTO llm_saved_material(material_id,saved_at) SELECT material_id,created_at FROM llm_material")
                db.execute("INSERT INTO llm_report_migration(key) VALUES ('legacy_reports')")

    def general_chat_history(self, user_id, limit):
        with closing(connect(self.path)) as db:
            rows = db.execute(
                """SELECT id, question, answer FROM llm_general_chat_turn
                   WHERE user_id=? ORDER BY id DESC LIMIT ?""", (user_id, limit),
            ).fetchall()
            return [{**dict(row), "answer": json.loads(row["answer"])} for row in reversed(rows)]

    def save_general_chat_turn(self, user_id, last_id, question, answer, *, require_current=True):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous_id = db.execute(
                "SELECT MAX(id) FROM llm_general_chat_turn WHERE user_id=?", (user_id,),
            ).fetchone()[0]
            if require_current and previous_id != last_id:
                raise WorkflowError("chat_history_changed")
            cursor = db.execute(
                "INSERT INTO llm_general_chat_turn (user_id, question, answer) VALUES (?, ?, ?)",
                (user_id, question, json.dumps(answer, ensure_ascii=False)),
            )
            return {"id": cursor.lastrowid, "question": question, "answer": answer}

    def chat_snapshot(self, card_id, user_id, limit):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN")
            card = self._read_card(db, card_id)
            run = self._read(db, card["latest_run"]) if card["latest_run"] else None
            rows = db.execute(
                """SELECT id, run_id, revision, question, answer FROM llm_chat_turn
                   WHERE user_id=? AND card_id=? ORDER BY id DESC LIMIT ?""",
                (user_id, card_id, limit),
            ).fetchall()
            return card, run, [{**dict(row), "answer": json.loads(row["answer"])} for row in reversed(rows)]

    @staticmethod
    def _read_card(db, card_id):
        card = db.execute("SELECT * FROM llm_card WHERE id=?", (card_id,)).fetchone()
        if card is None:
            raise WorkflowError("card_not_found")
        return dict(card)

    def save_chat_turn(self, card_id, user_id, run_id, revision, last_id, question, answer, *, refresh=None):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            card = self._read_card(db, card_id)
            if card["latest_run"] != run_id or (run_id and self._read(db, run_id).revision != revision):
                raise WorkflowError("chat_card_changed")
            previous_id = db.execute(
                "SELECT MAX(id) FROM llm_chat_turn WHERE user_id=? AND card_id=?",
                (user_id, card_id),
            ).fetchone()[0]
            if previous_id != last_id:
                raise WorkflowError("chat_history_changed")
            if refresh is not None:
                request_key, identity = refresh
                if card["version"] != identity["version"]:
                    raise WorkflowError("stale_card_version")
                job, created = self._new_job(db, card_id, request_key, "collect", identity)
                answer = refresh_reply(job)
            cursor = db.execute(
                """INSERT INTO llm_chat_turn (card_id, user_id, run_id, revision, question, answer)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (card_id, user_id, run_id, revision, question, json.dumps(answer, ensure_ascii=False)),
            )
            turn = {"id": cursor.lastrowid, "question": question, "answer": answer}
            return (turn, job, created) if refresh is not None else turn

    def confirm_chat_edit(self, card_id, user_id, turn_id):
        with closing(connect(self.path)) as db:
            row = db.execute(
                "SELECT run_id, revision FROM llm_chat_turn WHERE id=? AND card_id=? AND user_id=?",
                (turn_id, card_id, user_id),
            ).fetchone()
        if row is None or row["run_id"] is None:
            raise WorkflowError("chat_edit_not_found")
        run_id, revision = row["run_id"], row["revision"]

        def apply(run, db):
            row = db.execute("SELECT answer FROM llm_chat_turn WHERE id=?", (turn_id,)).fetchone()
            answer = json.loads(row["answer"])
            edit = answer.get("edit")
            if not edit or edit["status"] != "pending":
                raise WorkflowError("chat_edit_not_pending")
            if run.status not in {"needs_review", "partial", "failed"}:
                raise WorkflowError("chat_edit_collection_running")
            active = effective_facts(run)
            old = next((fact for fact in active if fact.fact_id == edit["fact_id"]), None)
            if old is None or old.value != edit["before"]:
                raise WorkflowError("chat_card_changed")
            now = stamp()
            replacement = edited_fact(old, edit["after"], active).model_copy(update={
                "fact_id": f"fact_{uuid4().hex}", "origin": "manual_edit", "evidence": [],
                "reviewed_by": user_id, "reviewed_at": now, "last_checked_at": now,
                "freshness_status": "within_window",
            })
            run.retired_fact_ids.append(old.fact_id)
            run.facts.append(replacement)
            db.execute("INSERT INTO llm_review VALUES (?,?,?,?,?,?,?,?,?,?)", (
                f"review_{uuid4().hex}", run_id, revision + 1, old.fact_id, "manual_edit",
                user_id, now.isoformat(), "Подтверждено сотрудником в чате",
                old.model_dump_json(), replacement.model_dump_json(),
            ))
            edit["status"] = "applied"
            answer["text"] = "Правка применена и сохранена в истории карточки."
            db.execute("UPDATE llm_chat_turn SET answer=? WHERE id=?",
                       (json.dumps(answer, ensure_ascii=False), turn_id))

        self.mutate(run_id, revision, apply)

    @staticmethod
    def _check_target(db, institution, kind=None):
        card = db.execute("SELECT * FROM llm_card WHERE id=?", (institution.id,)).fetchone()
        if card is None:
            return LLMRepository._check_target(db, institution, kind)
        if card["version"] != institution.card_version:
            raise WorkflowError("stale_card_version")
        if card["name"] != institution.name:
            raise WorkflowError("institution_name_mismatch")
        return "draft"

    def _check_run(self, db, run):
        super()._check_run(db, run)
        card = db.execute("SELECT latest_run FROM llm_card WHERE id=?", (run.institution.id,)).fetchone()
        if card is not None and card[0] and card[0] != run.run_id:
            raise WorkflowError("research_superseded_by_new_collection")

    def create_card(self, name, city, website):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return self._create_card(db, name, city, website)

    def _create_card(self, db, name, city, website):
        name, city = " ".join(name.split()), " ".join(city.split())
        digest = hashlib.sha256(json.dumps([name.casefold(), city.casefold(), website], ensure_ascii=False).encode()).hexdigest()[:24]
        card_id = f"draft:{digest}"
        db.execute("INSERT OR IGNORE INTO llm_card(id,name,city,website,created_at) VALUES (?,?,?,?,?)",
                   (card_id, name, city, website, stamp().isoformat()))
        return self._read_card(db, card_id)

    def start_card_intake(self, name, city, website, request_key, source_options):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            matches = [dict(row) for row in db.execute("SELECT * FROM llm_card")
                       if website_key(row["website"]) == website_key(website)]
            if len(matches) > 1 or (matches and city and matches[0]["city"]
                                    and normalized(city) != normalized(matches[0]["city"])):
                raise WorkflowError("chat_collection_card_ambiguous")
            card = matches[0] if matches else self._create_card(db, name, city, website)
            identity = {"version": card["version"], "leadership_url": None, **source_options}
            previous = db.execute(
                "SELECT payload FROM llm_job WHERE card_id=? AND request_key=?", (card["id"], request_key),
            ).fetchone()
            if previous:
                job, _ = self._new_job(db, card["id"], request_key, "collect", identity)
                return card, job, False
            for row in db.execute("SELECT payload FROM llm_job WHERE card_id=? ORDER BY rowid DESC", (card["id"],)):
                job = json.loads(row[0])
                if job["status"] in {"queued", "running"}:
                    if job["task_type"] != "collect":
                        raise WorkflowError("card_job_already_running")
                    return card, job, False
            job, created = self._new_job(db, card["id"], request_key, "collect", identity)
            return card, job, created

    def cards(self):
        with closing(connect(self.path)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM llm_card ORDER BY created_at DESC")]

    def card(self, card_id):
        with closing(connect(self.path)) as db:
            return self._read_card(db, card_id)

    def has_card(self, card_id):
        with closing(connect(self.path)) as db:
            return db.execute("SELECT 1 FROM llm_card WHERE id=?", (card_id,)).fetchone() is not None

    def ensure_card_deletable(self, card_id):
        with closing(connect(self.path)) as db:
            self._read_card(db, card_id)
            jobs = db.execute("SELECT payload FROM llm_job WHERE card_id=?", (card_id,)).fetchall()
            if any(json.loads(row[0]).get("status") in {"queued", "running"} for row in jobs):
                raise WorkflowError("card_job_already_running")

    def delete_card(self, card_id, actor=""):
        material_ids = []
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            card = db.execute("SELECT id FROM llm_card WHERE id=?", (card_id,)).fetchone()
            if card is None:
                return False
            jobs = db.execute("SELECT payload FROM llm_job WHERE card_id=?", (card_id,)).fetchall()
            if any(json.loads(row[0]).get("status") in {"queued", "running"} for row in jobs):
                raise WorkflowError("card_job_already_running")
            material_ids = [row[0] for row in db.execute(
                "SELECT m.material_id FROM llm_material m JOIN llm_run r ON r.run_id=m.run_id WHERE r.institution_id=?",
                (card_id,),
            )]
            db.execute("INSERT INTO llm_purge_context(card_id) VALUES (?)", (card_id,))
            db.execute("UPDATE llm_card SET latest_run=NULL WHERE id=?", (card_id,))
            db.execute("DELETE FROM llm_chat_turn WHERE card_id=?", (card_id,))
            db.execute("""DELETE FROM llm_general_chat_turn WHERE EXISTS (
                SELECT 1 FROM json_each(llm_general_chat_turn.answer, '$.cards') AS saved_card
                WHERE json_extract(saved_card.value, '$.id')=?
            )""", (card_id,))
            db.execute("""DELETE FROM llm_saved_material WHERE material_id IN (
                SELECT m.material_id FROM llm_material m JOIN llm_run r ON r.run_id=m.run_id WHERE r.institution_id=?
            )""", (card_id,))
            db.execute("""DELETE FROM llm_material_fact WHERE material_id IN (
                SELECT m.material_id FROM llm_material m JOIN llm_run r ON r.run_id=m.run_id WHERE r.institution_id=?
            )""", (card_id,))
            db.execute("""DELETE FROM llm_review WHERE run_id IN (
                SELECT run_id FROM llm_run WHERE institution_id=?
            )""", (card_id,))
            db.execute("""DELETE FROM llm_run_input WHERE run_id IN (
                SELECT run_id FROM llm_run WHERE institution_id=?
            )""", (card_id,))
            db.execute("""DELETE FROM llm_material WHERE run_id IN (
                SELECT run_id FROM llm_run WHERE institution_id=?
            )""", (card_id,))
            db.execute("DELETE FROM llm_job WHERE card_id=?", (card_id,))
            db.execute("DELETE FROM llm_run WHERE institution_id=?", (card_id,))
            db.execute("DELETE FROM llm_card WHERE id=?", (card_id,))
            db.execute("DELETE FROM llm_purge_context WHERE card_id=?", (card_id,))
        presentation_dir = self.path.parent / "presentations"
        for material_id in material_ids:
            for suffix in (".pptx", ".docx"):
                try:
                    (presentation_dir / f"{material_id}{suffix}").unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("Could not remove exported material for card %s", card_id)
        return True

    def new_job(self, card_id, request_key, task_type, identity):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._read_card(db, card_id)
            return self._new_job(db, card_id, request_key, task_type, identity)

    @staticmethod
    def _new_job(db, card_id, request_key, task_type, identity):
        previous = db.execute("SELECT payload FROM llm_job WHERE card_id=? AND request_key=?", (card_id, request_key)).fetchone()
        if previous:
            job = json.loads(previous[0])
            if job["identity"] != identity or job["task_type"] != task_type:
                raise WorkflowError("request_key_payload_mismatch")
            return job, False
        for row in db.execute("SELECT payload FROM llm_job WHERE card_id=?", (card_id,)):
            if json.loads(row[0])["status"] in {"queued", "running"}:
                raise WorkflowError("card_job_already_running")
        job = {"job_id": f"job_{uuid4().hex}", "card_id": card_id, "task_type": task_type,
               "status": "queued", "progress": 0, "message": "Задание создано",
               "created_at": stamp().isoformat(), "identity": identity, "result": None}
        db.execute("INSERT INTO llm_job VALUES (?,?,?,?,?)", (
            job["job_id"], card_id, request_key, task_type, json.dumps(job, ensure_ascii=False)))
        return job, True

    def job(self, job_id):
        with closing(connect(self.path)) as db:
            row = db.execute("SELECT payload FROM llm_job WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise WorkflowError("job_not_found")
            return json.loads(row[0])

    def update_job(self, job_id, **changes):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM llm_job WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise WorkflowError("job_not_found")
            job = json.loads(row[0])
            job.update(changes)
            db.execute("UPDATE llm_job SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
            return job

    def set_latest(self, card_id, run_id):
        with closing(connect(self.path)) as db, db:
            run = self._read(db, run_id)
            if run.institution.id != card_id:
                raise WorkflowError("research_card_mismatch")
            db.execute("UPDATE llm_card SET latest_run=? WHERE id=?", (run_id, card_id))

    def card_job(self, card_id):
        with closing(connect(self.path)) as db:
            row = db.execute("SELECT payload FROM llm_job WHERE card_id=? ORDER BY rowid DESC LIMIT 1", (card_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def fail_interrupted_jobs(self):
        """Call once on single-worker server startup, never on each connection."""
        with closing(connect(self.path)) as db, db:
            for row in db.execute("SELECT id,payload FROM llm_job").fetchall():
                job = json.loads(row[1])
                if job["status"] in {"queued", "running"}:
                    job.update(status="failed", message="Сервер перезапущен. Запустите задание заново",
                               result={"code": "server_restarted"})
                    db.execute("UPDATE llm_job SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), row[0]))

    def materials(self):
        with closing(connect(self.path)) as db:
            rows = db.execute("""SELECT m.material_id,m.created_at,m.run_id,m.payload,c.name
                FROM llm_material m JOIN llm_run r ON r.run_id=m.run_id
                JOIN llm_card c ON c.id=r.institution_id
                JOIN llm_saved_material s ON s.material_id=m.material_id
                ORDER BY m.rowid DESC""").fetchall()
        result = []
        for row in rows:
            path = self.path.parent / "presentations" / f"{row['material_id']}.pptx"
            if not path.is_file():
                continue
            try:
                _, stale = self.material(row["material_id"])
            except WorkflowError:
                stale = True
            # Old/superseded material payloads retain their variant independently of current research.
            variant = json.loads(row["payload"]).get("presentation_variant", "detailed")
            label = "Краткая карточка" if variant == "compact" else "Подробная справка"
            result.append({"id": row["material_id"], "name": f"{label}: {row['name']}", "presentation_variant": variant,
                "format": "PPTX", "created_at": row["created_at"], "stale": stale,
                "download_url": f"/api/v1/ai/materials/{row['material_id']}/download",
                "preview_url": f"/api/v1/ai/materials/{row['material_id']}/preview",
                "brief_url": f"/api/v1/ai/materials/{row['material_id']}/brief"})
        return result

    def save_material_to_reports(self, material_id):
        with closing(connect(self.path)) as db, db:
            if not db.execute("SELECT 1 FROM llm_material WHERE material_id=?", (material_id,)).fetchone():
                raise WorkflowError("material_not_found")
            db.execute("INSERT OR IGNORE INTO llm_saved_material(material_id,saved_at) VALUES (?,?)",
                       (material_id, stamp().isoformat()))
        return {"saved": True}

    def remove_material_from_reports(self, material_id):
        with closing(connect(self.path)) as db, db:
            if not db.execute("SELECT 1 FROM llm_saved_material WHERE material_id=?", (material_id,)).fetchone():
                raise WorkflowError("report_not_found")
            db.execute("DELETE FROM llm_saved_material WHERE material_id=?", (material_id,))
        return {"deleted": True}
