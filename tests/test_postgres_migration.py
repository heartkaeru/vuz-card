"""PostgreSQL tests use a NEW disposable local database per test, never a remote one."""
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from backend.database.postgres.legacy import load_legacy,literal_assignment,name_key,payroll_amount
try:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from backend.database.postgres.db import migrate,set_actor,MIGRATION_DIR
    from backend.database.postgres.transfer import import_plan,validate
except ImportError:
    psycopg=None

REPO=Path(__file__).resolve().parents[1]


class LegacyPlanTests(unittest.TestCase):
    def test_real_first_iteration_plan(self):
        plan=load_legacy(REPO)
        self.assertEqual(plan['summary']['input_records'],10)
        self.assertEqual(plan['summary']['universities'],8)
        self.assertEqual(plan['summary']['exact_name_duplicates_merged'],2)
        self.assertEqual(len({name_key(u['name']) for u in plan['universities']}),8)
    def test_money_keeps_precision_and_missing_period(self):
        self.assertEqual(payroll_amount('4.8 млрд руб.'),Decimal('4800000000.0'))
        self.assertEqual(payroll_amount('12,5 млн руб.'),Decimal('12500000.0'))
        self.assertIsNone(payroll_amount('нет данных'))
    def test_python_source_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'untrusted.py'
            sentinel=Path(directory)/'executed'
            path.write_text(f'from pathlib import Path\nPath({str(sentinel)!r}).touch()\nDATA=[1,2]\n',encoding='utf-8')
            self.assertEqual(literal_assignment(path,'DATA'),[1,2])
            self.assertFalse(sentinel.exists())
    def test_ambiguous_same_name_aborts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'conflict.json'
            name='Тестовый университет'
            path.write_text(json.dumps([{'name':name,'city':'Город А'},{'name':name,'city':'Город Б'}],ensure_ascii=False),encoding='utf-8')
            with self.assertRaises(ValueError):
                load_legacy(REPO,[path])


@unittest.skipUnless(psycopg and os.environ.get('VUZ_TEST_DATABASE_URL'),
                     'Set VUZ_TEST_DATABASE_URL to a disposable LOCAL PostgreSQL admin connection')
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.options=conninfo_to_dict(os.environ['VUZ_TEST_DATABASE_URL'])
        if cls.options.get('host') not in ('127.0.0.1','localhost','::1'):
            raise unittest.SkipTest('Tests refuse non-local database hosts')
        cls.options['dbname']='postgres'
        cls.admin=psycopg.connect(**cls.options,autocommit=True)
        for role in ('anon','authenticated','service_role'):
            if not cls.admin.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                cls.admin.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
    def setUp(self):
        self.database='vuz_test_'+uuid4().hex
        self.admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.database)))
        self.conn=psycopg.connect(**{**self.options,'dbname':self.database})
        # Simulate the broader default grants of a managed project, then check our revocations.
        self.conn.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon,authenticated,service_role')
        self.conn.commit()
        migrate(self.conn)
        self.plan=load_legacy(REPO)
    def tearDown(self):
        self.conn.close()
        self.admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.database)))
    def seed(self):
        return import_plan(self.conn,self.plan)
    def test_migrations_repeat_without_changes(self):
        self.assertEqual(migrate(self.conn),[])
        self.assertEqual(validate(self.conn)['unvalidated_constraints'],0)
    def test_modified_migration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for p in MIGRATION_DIR.glob('*.sql'):
                (root/p.name).write_bytes(p.read_bytes()+b'\n-- accidental edit\n')
            with self.assertRaises(ValueError):
                migrate(self.conn,root)
    def test_transfer_counts_and_relations(self):
        result=self.seed()['validation']
        self.assertEqual(result['vuz_universities'],8)
        self.assertEqual(result['vuz_officials'],6)
        self.assertEqual(result['vuz_official_contacts'],12)
        self.assertEqual(result['vuz_university_metrics'],6)
        self.assertEqual(result['vuz_projects'],7)
        self.assertEqual(result['duplicate_names'],0)
        self.assertEqual(result['orphan_officials'],0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM public.vuz_officials WHERE verification_status='verified'").fetchone()[0],0)
    def test_repeat_does_not_create_duplicates_or_audit_noise(self):
        self.seed()
        first=validate(self.conn)
        second=self.seed()
        self.assertTrue(all(n==0 for n in second['inserted'].values()))
        self.assertEqual(first,validate(self.conn))
    def test_repeat_import_uses_bounded_statement_count(self):
        self.seed()
        class CountingConnection:
            def __init__(self,conn):
                self.conn=conn; self.calls=0
            def execute(self,*args,**kwargs):
                self.calls+=1
                return self.conn.execute(*args,**kwargs)
            def __getattr__(self,name):
                return getattr(self.conn,name)
        counted=CountingConnection(self.conn)
        result=import_plan(counted,self.plan)
        self.assertFalse(any(result['inserted'].values()))
        self.assertLessEqual(counted.calls,25)

    def test_names_unique_after_whitespace_case_normalization(self):
        self.seed()
        with self.assertRaises(psycopg.errors.UniqueViolation),self.conn.transaction():
            self.conn.execute("INSERT INTO public.vuz_universities(id,name) VALUES (%s,%s)",
                              (uuid4(),'  байкальский   государственный университет  '))
    def test_missing_parent_rejected_by_foreign_key(self):
        self.seed()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation),self.conn.transaction():
            self.conn.execute("INSERT INTO public.vuz_official_contacts(id,official_id,channel_type,value_raw,value_normalized,source_id) SELECT %s,%s,'email','x@example.test','x@example.test',id FROM public.vuz_sources LIMIT 1",(uuid4(),uuid4()))
    def test_demo_metrics_are_exact_and_not_approved(self):
        self.seed()
        row=self.conn.execute("SELECT value,period_start,verification_status FROM public.vuz_university_metrics WHERE raw_value='4.8 млрд руб.'").fetchone()
        self.assertEqual(row,(Decimal('4800000000.000000'),None,'needs_review'))
        with self.assertRaises(psycopg.errors.CheckViolation),self.conn.transaction():
            self.conn.execute("UPDATE public.vuz_university_metrics SET verification_status='verified' WHERE raw_value='4.8 млрд руб.'")
    def test_human_audit_stores_user_old_new_and_version(self):
        self.seed()
        actor=uuid4()
        self.conn.execute("INSERT INTO public.vuz_users(id,email,display_name,role) VALUES (%s,'test@example.test','Tester','editor')",(actor,))
        self.conn.commit()
        record=self.conn.execute('SELECT id,city,version FROM public.vuz_universities ORDER BY name LIMIT 1').fetchone()
        with self.conn.transaction():
            set_actor(self.conn,'human','test editor',actor)
            self.conn.execute('UPDATE public.vuz_universities SET city=%s WHERE id=%s',('Updated test city',record[0]))
        row=self.conn.execute("SELECT old_value,new_value,record_version,actor_id,actor_type FROM public.vuz_audit_log WHERE record_id=%s AND field_name='city' ORDER BY id DESC LIMIT 1",(record[0],)).fetchone()
        self.assertEqual(row,(record[1],'Updated test city',record[2]+1,actor,'human'))
    def test_ai_audit(self):
        self.seed()
        with self.conn.transaction():
            set_actor(self.conn,'ai','agent test')
            self.conn.execute("UPDATE public.vuz_universities SET profile='Agent draft' WHERE id=(SELECT id FROM public.vuz_universities LIMIT 1)")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM public.vuz_audit_log WHERE actor_type='ai' AND field_name='profile'").fetchone()[0],1)
    def test_audit_is_append_only(self):
        self.seed()
        with self.assertRaises(psycopg.Error),self.conn.transaction():
            self.conn.execute('DELETE FROM public.vuz_audit_log')
    def test_stale_version_is_rejected(self):
        self.seed()
        row=self.conn.execute('SELECT id,version FROM public.vuz_universities LIMIT 1').fetchone()
        with self.assertRaises(psycopg.errors.SerializationFailure),self.conn.transaction():
            self.conn.execute("SELECT set_config('vuz.expected_version','999999',true)")
            self.conn.execute("UPDATE public.vuz_universities SET city='Wrong city' WHERE id=%s",(row[0],))
        self.assertEqual(self.conn.execute('SELECT version FROM public.vuz_universities WHERE id=%s',(row[0],)).fetchone()[0],row[1])
    def test_existing_manual_edit_not_overwritten_by_import(self):
        self.seed()
        self.conn.execute("UPDATE public.vuz_universities SET city='Manual city' WHERE id=%s",(self.plan['universities'][0]['id'],))
        self.conn.commit()
        before=validate(self.conn)
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(validate(self.conn),before)
    def test_anonymous_role_cannot_read_cards(self):
        self.seed()
        with self.assertRaises(psycopg.errors.InsufficientPrivilege),self.conn.transaction():
            self.conn.execute('SET LOCAL ROLE anon')
            self.conn.execute('SELECT * FROM public.vuz_universities')
    def test_viewer_cannot_write_or_escalate_role(self):
        self.seed()
        subject=uuid4()
        self.conn.execute("INSERT INTO public.vuz_users(auth_subject,email,display_name,role) VALUES (%s,'viewer@example.test','Viewer','viewer')",(subject,))
        self.conn.commit()
        with self.conn.transaction():
            self.conn.execute('SET LOCAL ROLE authenticated')
            self.conn.execute("SELECT set_config('vuz.actor_id','',true),set_config('request.jwt.claims',%s,true)",(json.dumps({'sub':str(subject)}),))
            self.assertEqual(self.conn.execute('SELECT count(*) FROM public.vuz_universities').fetchone()[0],8)
            self.assertEqual(self.conn.execute("UPDATE public.vuz_universities SET city='Forbidden'").rowcount,0)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege),self.conn.transaction():
                self.conn.execute("UPDATE public.vuz_users SET role='admin'")
    def test_editor_can_edit_but_cannot_insert_fake_audit(self):
        self.seed()
        subject=uuid4()
        self.conn.execute("INSERT INTO public.vuz_users(auth_subject,email,display_name,role) VALUES (%s,'editor@example.test','Editor','editor')",(subject,))
        self.conn.commit()
        with self.conn.transaction():
            self.conn.execute('SET LOCAL ROLE authenticated')
            self.conn.execute("SELECT set_config('vuz.actor_id','',true),set_config('request.jwt.claims',%s,true)",(json.dumps({'sub':str(subject)}),))
            self.assertEqual(self.conn.execute("UPDATE public.vuz_universities SET city='Allowed' WHERE id=(SELECT id FROM public.vuz_universities LIMIT 1)").rowcount,1)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege),self.conn.transaction():
                self.conn.execute("INSERT INTO public.vuz_audit_log(table_name,record_id,operation,field_name,record_version,actor_type,actor_label,database_role) VALUES ('fake',%s,'INSERT','fake',1,'human','fake','fake')",(uuid4(),))

if __name__=='__main__':
    unittest.main()
