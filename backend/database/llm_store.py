"""Additive LLM staging tables. Existing registry schema/user_version stay unchanged."""

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schema import connect, execute_statements, initialize
from app.services.llm.models import DraftResult, Fact, Institution
from app.services.llm.workflow_models import ResearchRun, WorkflowError
from app.services.llm.changes import effective_facts


def stamp():
    return datetime.now(timezone.utc)


class LLMRepository:
    def __init__(self, path: Path):
        self.path = Path(path)
        with closing(connect(self.path)) as db:
            initialize(db)
            with db:
                db.execute("BEGIN IMMEDIATE")
                execute_statements(db, '''
                CREATE TABLE IF NOT EXISTS llm_workflow_schema (
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
                INSERT OR IGNORE INTO llm_workflow_schema VALUES (1,1);
                CREATE TABLE IF NOT EXISTS llm_run (
                  run_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL, request_key TEXT NOT NULL,
                  input_hash TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
                  payload TEXT NOT NULL CHECK(json_valid(payload)),
                  UNIQUE(institution_id,request_key));
                CREATE TABLE IF NOT EXISTS llm_run_input (
                  run_id TEXT PRIMARY KEY REFERENCES llm_run(run_id),
                  payload TEXT NOT NULL CHECK(json_valid(payload)));
                CREATE TABLE IF NOT EXISTS llm_review (
                  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES llm_run(run_id),
                  revision INTEGER NOT NULL, fact_id TEXT NOT NULL, decision TEXT NOT NULL,
                  actor TEXT NOT NULL, reviewed_at TEXT NOT NULL, reason TEXT NOT NULL,
                  before_json TEXT NOT NULL CHECK(json_valid(before_json)),
                  after_json TEXT NOT NULL CHECK(json_valid(after_json)),
                  UNIQUE(run_id,revision,fact_id));
                CREATE TABLE IF NOT EXISTS llm_material (
                  material_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES llm_run(run_id),
                  fact_revision INTEGER NOT NULL, created_at TEXT NOT NULL,
                  payload TEXT NOT NULL CHECK(json_valid(payload)));
                CREATE TABLE IF NOT EXISTS llm_material_fact (
                  material_id TEXT NOT NULL REFERENCES llm_material(material_id),
                  fact_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                  payload TEXT NOT NULL CHECK(json_valid(payload)), PRIMARY KEY(material_id,fact_id));
                CREATE TRIGGER IF NOT EXISTS llm_material_fact_no_update BEFORE UPDATE ON llm_material_fact
                  BEGIN SELECT RAISE(ABORT,'Material fact snapshot is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS llm_material_fact_no_delete BEFORE DELETE ON llm_material_fact
                  BEGIN SELECT RAISE(ABORT,'Material fact snapshot is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS llm_review_no_update BEFORE UPDATE ON llm_review
                  BEGIN SELECT RAISE(ABORT,'LLM review audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS llm_review_no_delete BEFORE DELETE ON llm_review
                  BEGIN SELECT RAISE(ABORT,'LLM review audit is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS llm_input_no_update BEFORE UPDATE ON llm_run_input
                  BEGIN SELECT RAISE(ABORT,'LLM source snapshot is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS llm_input_no_delete BEFORE DELETE ON llm_run_input
                  BEGIN SELECT RAISE(ABORT,'LLM source snapshot is immutable'); END;
                ''')
                if db.execute("SELECT version FROM llm_workflow_schema WHERE singleton=1").fetchone()[0] != 1:
                    raise WorkflowError("unsupported_llm_storage_version")

    @staticmethod
    def _read(db, run_id):
        row = db.execute("SELECT payload FROM llm_run WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError("run_not_found")
        return ResearchRun.model_validate_json(row[0])

    @staticmethod
    def _check_target(db, institution: Institution, kind=None):
        row = db.execute("SELECT display_name,version FROM institution WHERE id=?", (institution.id,)).fetchone()
        if row is None:
            if kind == "registered":
                raise WorkflowError("institution_not_found")
            if not institution.id.startswith("draft:") or institution.card_version != 1:
                raise WorkflowError("unknown_institution_use_explicit_draft_id")
            return "draft"
        if kind == "draft":
            raise WorkflowError("draft_identity_requires_explicit_rebinding")
        if row[1] != institution.card_version:
            raise WorkflowError("stale_card_version")
        if row[0] != institution.name:
            raise WorkflowError("institution_name_mismatch")
        return "registered"

    def create(self, institution: Institution, card_payload: dict, request_key: str, modes=("0.2", "0.3")):
        if not request_key.strip():
            raise WorkflowError("request_key_required")
        payload = json.dumps({"institution": institution.model_dump(mode="json"), "card": card_payload, "modes": list(modes)},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            kind = self._check_target(db, institution)
            old = db.execute("SELECT run_id,input_hash FROM llm_run WHERE institution_id=? AND request_key=?",
                             (institution.id, request_key)).fetchone()
            if old:
                stored = json.loads(db.execute("SELECT payload FROM llm_run_input WHERE run_id=?", (old[0],)).fetchone()[0])
                old_request = {key: stored[key] for key in ("institution", "card", "modes")}
                if json.dumps(old_request, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != payload:
                    raise WorkflowError("request_key_payload_mismatch")
                return self._read(db, old[0]), False
            previous = self._previous_run(db, institution)
            baseline = [f.model_copy(deep=True) for f in effective_facts(previous)] if previous else []
            run = ResearchRun(run_id=f"run_{uuid4().hex}", institution=institution, target_kind=kind,
                              created_at=stamp(), updated_at=stamp(),
                              requested_modes=list(modes),
                              parser_warnings=card_payload.get("warnings", []),
                              parser_errors=card_payload.get("errors", []),
                              previous_run_id=previous.run_id if previous else None, baseline_facts=baseline)
            frozen = json.loads(payload)
            frozen.update(previous_run_id=run.previous_run_id, baseline_facts=[f.model_dump(mode="json") for f in baseline])
            payload = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            db.execute("INSERT INTO llm_run VALUES (?,?,?,?,?,?)", (
                run.run_id, institution.id, request_key, digest, run.revision, run.model_dump_json()))
            db.execute("INSERT INTO llm_run_input VALUES (?,?)", (run.run_id, payload))
            self._after_create(db, run)
            return run, True

    def _previous_run(self, db, institution):
        row = db.execute("SELECT run_id FROM llm_run WHERE institution_id=? ORDER BY rowid DESC LIMIT 1", (institution.id,)).fetchone()
        previous = self._read(db, row[0]) if row else None
        return previous if previous and previous.institution == institution else None

    def _after_create(self, db, run):
        pass

    def history(self, institution_id):
        with closing(connect(self.path)) as db:
            return [self._read(db, row[0]) for row in db.execute(
                "SELECT run_id FROM llm_run WHERE institution_id=? ORDER BY rowid DESC LIMIT 20", (institution_id,))]

    def get(self, run_id):
        with closing(connect(self.path)) as db:
            return self._read(db, run_id)

    def _check_run(self, db, run):
        self._check_target(db, run.institution, run.target_kind)

    def source_card(self, run_id):
        with closing(connect(self.path)) as db:
            self._read(db, run_id)
            return json.loads(db.execute("SELECT payload FROM llm_run_input WHERE run_id=?", (run_id,)).fetchone()[0])["card"]

    def mutate(self, run_id, revision, operation, *, check_target=True):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = self._read(db, run_id)
            if run.revision != revision:
                raise WorkflowError("stale_run_revision")
            if check_target:
                self._check_run(db, run)
            operation(run, db)
            run.revision += 1
            run.updated_at = stamp()
            # Revalidate mutable copies before persistence.
            run = ResearchRun.model_validate_json(run.model_dump_json())
            db.execute("UPDATE llm_run SET revision=?,payload=? WHERE run_id=?", (
                run.revision, run.model_dump_json(), run_id))
            return run

    def check_current(self, run_id, revision):
        with closing(connect(self.path)) as db:
            run = self._read(db, run_id)
            if run.revision != revision:
                raise WorkflowError("stale_run_revision")
            self._check_run(db, run)
            return run

    def save_material(self, run_id, revision, draft: DraftResult):
        with closing(connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = self._read(db, run_id)
            if run.revision != revision:
                raise WorkflowError("facts_changed_during_generation")
            self._check_run(db, run)
            material_id = f"material_{uuid4().hex}"
            db.execute("INSERT INTO llm_material VALUES (?,?,?,?,?)", (
                material_id, run_id, revision, stamp().isoformat(), draft.model_dump_json()))
            by_id = {fact.fact_id: fact for fact in effective_facts(run)}
            if not draft.sources.keys() <= by_id.keys():
                raise WorkflowError("unknown_material_fact")
            for index, fact_id in enumerate(draft.sources):
                db.execute("INSERT INTO llm_material_fact VALUES (?,?,?,?)", (
                    material_id, fact_id, index, by_id[fact_id].model_dump_json()))
            return material_id

    def material_facts(self, material_id):
        with closing(connect(self.path)) as db:
            if not db.execute("SELECT 1 FROM llm_material WHERE material_id=?", (material_id,)).fetchone():
                raise WorkflowError("material_not_found")
            return [Fact.model_validate_json(row[0]) for row in db.execute(
                "SELECT payload FROM llm_material_fact WHERE material_id=? ORDER BY ordinal", (material_id,))]

    def material(self, material_id):
        with closing(connect(self.path)) as db:
            row = db.execute("SELECT run_id,fact_revision,payload FROM llm_material WHERE material_id=?", (material_id,)).fetchone()
            if row is None:
                raise WorkflowError("material_not_found")
            run = self._read(db, row[0])
            self._check_run(db, run)
            return DraftResult.model_validate_json(row[2]), run.revision != row[1]

    def reviews(self, run_id):
        with closing(connect(self.path)) as db:
            self._read(db, run_id)
            return [dict(row) for row in db.execute("SELECT * FROM llm_review WHERE run_id=? ORDER BY rowid", (run_id,))]
