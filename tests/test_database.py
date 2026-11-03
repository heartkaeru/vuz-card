"""Run: python -m unittest discover -s tests -v. Fixtures are synthetic."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from backend.database.schema import connect, initialize
from backend.database.intake import import_file, inn_valid
from backend.database.review import load_identities, summary, write_report


def synthetic_inn(prefix='123456789'):
    return prefix+str(sum(int(x)*w for x,w in zip(prefix,(2,4,10,3,5,9,4,6,8)))%11%10)


class Sheet:
    title='Контакты'
    def __init__(self, rows):
        self.rows=rows
        self.max_row=len(rows)
    def cell(self,r,c):
        value=self.rows[r-1][c-1] if c<=len(self.rows[r-1]) else None
        return SimpleNamespace(value=value,coordinate=f'{chr(64+c)}{r}',row=r,column=c,
             data_type='f' if isinstance(value,str) and value.startswith('=') else 's',number_format='General')
    def __iter__(self):
        return iter([[self.cell(r,c) for c in range(1,9)] for r in range(1,len(self.rows)+1)])
    def iter_rows(self,min_row,max_row):
        return iter(list(self)[min_row-1:max_row])


class Workbook(list):
    def close(self):
        pass


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.db=connect(self.root/'test.sqlite3')
        initialize(self.db)
        self.rows=[['Регион','Учебное заведение','ВУЗ/СПО','Точка контакта','Контактное лицо','Эл. почта','Телефон','ИНН'],
          ['Регион А','Синтетический вуз','ВУЗ','Центр карьеры'],
          ['Регион А','Синтетический вуз','ВУЗ',' <office@example.test> '],
          [None,None,None,'Директор без организации']]
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def ingest(self,name='a.xlsx',rows=None,blob=b'fixture-one'):
        path=self.root/name
        path.write_bytes(blob)
        with patch('backend.database.intake.load_workbook',return_value=Workbook([Sheet(rows or self.rows)])):
            return import_file(self.db,path,'Синтетический отдел')
    def resolve(self,**overrides):
        entry={'source_name':'Синтетический вуз','source_region':'Регион А',
          'inn':synthetic_inn(),'official_name':'Синтетическое юридическое лицо',
          'unit_code':'head','unit_type':'head','education_type':'university',
          'verified_by':'test-reviewer','verified_at':'2026-11-02T12:00:00+05:00',
          'evidence':'synthetic test fixture; not real organizational data'}
        entry.update(overrides)
        path=self.root/'identities.json'
        path.write_text(json.dumps([entry],ensure_ascii=False),encoding='utf-8')
        return load_identities(self.db,path)
    def test_schema_and_fk(self):
        initialize(self.db)
        self.assertEqual(self.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO contact_point(id,institution_id,title_raw,title_normalized) VALUES ('x','missing','x','x')")
    def test_invalid_inn_rejected_by_sql(self):
        self.assertTrue(inn_valid(synthetic_inn()))
        self.assertFalse(inn_valid('0000000000'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO legal_entity(id,inn,official_name,verified_at) VALUES ('x','1234567890','invalid','today')")
    def test_missing_identity_and_orphan_preserved(self):
        result=self.ingest()
        self.assertEqual((result['data_rows'],result['review']),(3,3))
        self.assertEqual(summary(self.db)['registry_candidate'],1)
        self.assertEqual(summary(self.db)['legal_entity'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_record').fetchone()[0],4)
        orphan=self.db.execute("SELECT * FROM staging_record WHERE candidate_id IS NULL AND row_type='data'").fetchone()
        self.assertIsNotNone(orphan)
    def test_same_file_idempotent(self):
        self.ingest()
        self.ingest()
        result=summary(self.db)
        self.assertEqual(result['source'],1)
        self.assertEqual(result['source_record'],4)
        self.assertEqual(result['registry_candidate'],1)
        self.assertEqual(result['import_batch'],2)  # attempts are intentionally auditable
    def test_two_departments_preserve_provenance_and_merge_candidates(self):
        self.ingest()
        self.ingest('b.xlsx',blob=b'fixture-two')
        self.assertEqual(summary(self.db)['source'],2)
        self.assertEqual(summary(self.db)['registry_candidate'],1)
        row=self.db.execute('SELECT source_rows,source_files FROM candidate_registry').fetchone()
        self.assertEqual(tuple(row),(4,2))
    def test_identity_then_contact_import_is_idempotent(self):
        self.ingest()
        self.resolve()
        result=self.ingest()
        self.assertEqual(result['applied'],2)
        first=summary(self.db)
        self.assertEqual((first['legal_entity'],first['institution'],first['contact_point'],first['contact_channel']),(1,1,2,1))
        logs=self.db.execute('SELECT COUNT(*) FROM change_log').fetchone()[0]
        self.resolve()
        self.ingest()
        self.assertEqual(summary(self.db)['contact_channel'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM change_log').fetchone()[0],logs)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
    def test_invalid_mapping_atomic(self):
        self.ingest()
        with self.assertRaises(ValueError):
            self.resolve(inn='1234567890')
        self.assertEqual(summary(self.db)['legal_entity'],0)
    def test_changed_identity_not_silently_reassigned(self):
        self.ingest()
        self.resolve()
        with self.assertRaises(ValueError):
            self.resolve(inn=synthetic_inn('123456788'))
        self.assertEqual(summary(self.db)['legal_entity'],1)
    def test_formula_blocks_application(self):
        self.ingest()
        self.resolve()
        rows=[self.rows[0],['Регион А','Синтетический вуз','ВУЗ','=A1']]
        result=self.ingest('formula.xlsx',rows,b'formula')
        self.assertEqual(result['applied'],0)
        self.assertEqual(summary(self.db)['contact_point'],0)
    def test_conflicting_inn_blocks_mapping(self):
        self.ingest()
        self.resolve()
        rows=[self.rows[0],['Регион А','Синтетический вуз','ВУЗ','Ректор',None,None,None,synthetic_inn('123456788')]]
        result=self.ingest('conflict.xlsx',rows,b'conflict')
        self.assertEqual(result['applied'],0)
        self.assertEqual(summary(self.db)['contact_point'],0)
    def test_unknown_template_does_not_register_success(self):
        with self.assertRaises(ValueError):
            self.ingest(rows=[['Unknown','Template']],blob=b'unknown')
        self.assertEqual(summary(self.db)['source'],0)
    def test_unit_same_inn_separate_cards(self):
        self.ingest()
        self.resolve()
        legal=self.db.execute('SELECT id FROM legal_entity').fetchone()[0]
        self.db.execute("INSERT INTO institution(id,legal_entity_id,unit_code,display_name,education_type,unit_type) VALUES (?,?,?,?,?,?)",
          ('branch',legal,'branch','Синтетический филиал','university','branch'))
        self.assertEqual(summary(self.db)['institution'],2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE institution SET parent_institution_id=id WHERE id='branch'")
    def test_money_is_exact_and_currency_required(self):
        self.ingest()
        self.resolve()
        institution=self.db.execute('SELECT id FROM institution').fetchone()[0]
        values=['money',institution,'payroll_fund','2026-01-01','2026-01-31','month','period_total','unit','employees','actual',12345,'RUB','gross',1,1,'verified']
        sql='''INSERT INTO payroll_metric(id,institution_id,metric_code,period_start,period_end,period_kind,aggregation_kind,
        scope_code,population_code,scenario,amount_minor,currency,methodology_code,revision_no,is_current,verification_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
        self.db.execute(sql,values)
        self.assertEqual(self.db.execute('SELECT amount_minor FROM payroll_metric').fetchone()[0],12345)
        with self.assertRaises(sqlite3.IntegrityError):
            values[0]='duplicate'
            self.db.execute(sql,values)
        with self.assertRaises(sqlite3.IntegrityError):
            values[0]='null-currency'; values[11]=None
            self.db.execute(sql,values)
    def test_reports_and_audit_immutable(self):
        self.ingest()
        self.resolve()
        self.ingest()
        write_report(self.db,self.root/'reports')
        self.assertTrue((self.root/'reports'/'IMPORT_REPORT.md').exists())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM change_log')


if __name__=='__main__':
    unittest.main()
