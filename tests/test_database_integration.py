import csv
import json

import pytest
from openpyxl import Workbook

from backend.database.intake import import_file
from backend.database.review import load_identities, summary, write_report
from backend.database.schema import connect, initialize


@pytest.fixture
def excel_registry(tmp_path):
    db = connect(tmp_path / "synthetic.sqlite3")
    initialize(db)
    def prepare(phone=None, education_type="ВУЗ"):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Регион", "Учебное заведение", "ВУЗ/СПО", "Точка контакта", "Эл. почта", "Телефон"])
        sheet.append(["Регион А", "Синтетический вуз", education_type, "Ректор", "office@example.test", phone])
        path = tmp_path / "synthetic.xlsx"
        workbook.save(path)
        workbook.close()
        import_file(db, path)
        mapping = [{"source_name": "Синтетический вуз", "source_region": "Регион А",
                    "inn": "1234567894", "official_name": "Синтетический вуз",
                    "unit_code": "head", "unit_type": "head", "verified_by": "test",
                    "verified_at": "2026-11-03T12:00:00+05:00", "evidence": "synthetic fixture"}]
        mapping_path = tmp_path / "mapping.json"
        mapping_path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        load_identities(db, mapping_path)
        return path
    yield db, prepare
    db.close()


@pytest.mark.parametrize("phone, education_type", [("+7 495 123-45-67", "ВУЗ"), (None, None)])
def test_applied_row_with_open_issues_stays_in_review_queue(excel_registry, tmp_path, phone, education_type):
    db, prepare = excel_registry
    path = prepare(phone, education_type)
    result = import_file(db, path)
    assert result["applied"] == 1
    assert result["review_required"] == 1
    assert summary(db)["latest_batches"][0]["status"] == "completed_with_review"
    write_report(db, tmp_path / "reports")
    with (tmp_path / "reports/review_queue.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["errors"] != "[]"


def test_real_xlsx_roundtrip_keeps_pending_contacts_and_is_idempotent(excel_registry, tmp_path):
    db, prepare = excel_registry
    path = prepare()
    result = import_file(db, path)
    before = summary(db)
    import_file(db, path)
    after = summary(db)
    for table in ("source_record", "contact_point", "contact_channel"):
        assert before[table] == after[table]
    assert result["review_required"] == 0
    assert db.execute("SELECT verification_status FROM contact_channel").fetchone()[0] == "pending"
    write_report(db, tmp_path / "reports")
    with (tmp_path / "reports/review_queue.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream, delimiter=";")) == []
