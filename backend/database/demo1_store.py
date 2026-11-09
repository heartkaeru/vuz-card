"""Persistent draft cards and jobs for Demo 1; no fabricated legal entity records."""

import hashlib
import json
from contextlib import closing
from uuid import uuid4

from .llm_store import LLMRepository, stamp
from .schema import connect, execute_statements
from app.services.llm.workflow_models import WorkflowError


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
            ''')

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
        name, city = " ".join(name.split()), " ".join(city.split())
        digest = hashlib.sha256(json.dumps([name.casefold(), city.casefold(), website], ensure_ascii=False).encode()).hexdigest()[:24]
        card_id = f"draft:{digest}"
        with closing(connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO llm_card(id,name,city,website,created_at) VALUES (?,?,?,?,?)",
                       (card_id, name, city, website, stamp().isoformat()))
        return self.card(card_id)

    def cards(self):
        with closing(connect(self.path)) as db:
            return [dict(row) for row in db.execute("SELECT * FROM llm_card ORDER BY created_at DESC")]

    def card(self, card_id):
        with closing(connect(self.path)) as db:
            row = db.execute("SELECT * FROM llm_card WHERE id=?", (card_id,)).fetchone()
            if row is None:
                raise WorkflowError("card_not_found")
            return dict(row)

    def new_job(self, card_id, request_key, task_type, identity):
        self.card(card_id)
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
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
                JOIN llm_card c ON c.id=r.institution_id ORDER BY m.rowid DESC""").fetchall()
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
