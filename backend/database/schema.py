"""SQLite physical schema. Money is stored in minor units, never REAL."""
import sqlite3
from pathlib import Path

TABLES = {
'region': '''code TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE''',
'department': '''id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE''',
'legal_entity': '''id TEXT PRIMARY KEY, inn TEXT NOT NULL UNIQUE
 CHECK(length(inn)=10 AND inn NOT GLOB '*[^0-9]*' AND inn<>'0000000000'
 AND CAST(substr(inn,10,1) AS INTEGER)=((2*CAST(substr(inn,1,1) AS INTEGER)
 +4*CAST(substr(inn,2,1) AS INTEGER)+10*CAST(substr(inn,3,1) AS INTEGER)
 +3*CAST(substr(inn,4,1) AS INTEGER)+5*CAST(substr(inn,5,1) AS INTEGER)
 +9*CAST(substr(inn,6,1) AS INTEGER)+4*CAST(substr(inn,7,1) AS INTEGER)
 +6*CAST(substr(inn,8,1) AS INTEGER)+8*CAST(substr(inn,9,1) AS INTEGER))%11)%10),
 official_name TEXT NOT NULL, short_name TEXT, ogrn TEXT CHECK(ogrn IS NULL OR
 (length(ogrn)=13 AND ogrn NOT GLOB '*[^0-9]*')), head_kpp TEXT, legal_address TEXT,
 status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('active','reorganized','closed','unknown')),
 verified_at TEXT NOT NULL''',
'institution': '''id TEXT PRIMARY KEY, legal_entity_id TEXT NOT NULL REFERENCES legal_entity(id),
 parent_institution_id TEXT REFERENCES institution(id), unit_code TEXT NOT NULL,
 display_name TEXT NOT NULL, education_type TEXT NOT NULL CHECK(education_type IN ('university','spo','mixed','unknown')),
 unit_type TEXT NOT NULL CHECK(unit_type IN ('head','branch','college','other')),
 kpp TEXT CHECK(kpp IS NULL OR length(kpp)=9), region_code TEXT REFERENCES region(code),
 city TEXT, address TEXT, website TEXT, profile TEXT, owner_user_id TEXT,
 status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('active','inactive','unknown')),
 UNIQUE(legal_entity_id,unit_code), CHECK(parent_institution_id IS NULL OR parent_institution_id<>id)''',
'institution_alias': '''id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institution(id),
 alias_raw TEXT NOT NULL, alias_normalized TEXT NOT NULL, source_system TEXT NOT NULL,
 external_id TEXT, valid_from TEXT, valid_to TEXT, UNIQUE(institution_id,alias_normalized,source_system),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from)''',
'contact_point': '''id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institution(id),
 department_name TEXT, title_raw TEXT NOT NULL, title_normalized TEXT NOT NULL,
 responsibility TEXT, point_type TEXT NOT NULL DEFAULT 'function'
 CHECK(point_type IN ('position','department','function')),
 verification_status TEXT NOT NULL DEFAULT 'pending' CHECK(verification_status IN ('pending','verified','conflict','rejected')),
 UNIQUE(institution_id,title_normalized)''',
'person': '''id TEXT PRIMARY KEY, full_name TEXT NOT NULL, last_name TEXT, first_name TEXT,
 middle_name TEXT, biography TEXT, academic_degree TEXT,
 verification_status TEXT NOT NULL DEFAULT 'pending' CHECK(verification_status IN ('pending','verified','conflict','rejected'))''',
'appointment': '''id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES person(id),
 contact_point_id TEXT NOT NULL REFERENCES contact_point(id), valid_from TEXT, valid_to TEXT,
 appointment_status TEXT NOT NULL DEFAULT 'unknown' CHECK(appointment_status IN ('current','former','unknown')),
 is_acting INTEGER NOT NULL DEFAULT 0 CHECK(is_acting IN (0,1)), verified_at TEXT,
 verification_status TEXT NOT NULL DEFAULT 'pending' CHECK(verification_status IN ('pending','verified','conflict','rejected')),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from)''',
'contact_channel': '''id TEXT PRIMARY KEY, contact_point_id TEXT REFERENCES contact_point(id),
 appointment_id TEXT REFERENCES appointment(id), channel_type TEXT NOT NULL CHECK(channel_type IN ('email','phone')),
 value_raw TEXT NOT NULL, value_normalized TEXT NOT NULL, extension TEXT, contact_label TEXT,
 valid_from TEXT, valid_to TEXT, verified_at TEXT,
 verification_status TEXT NOT NULL DEFAULT 'pending' CHECK(verification_status IN ('pending','verified','conflict','rejected')),
 CHECK((contact_point_id IS NULL)<>(appointment_id IS NULL)),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from)''',
'metric_definition': '''code TEXT PRIMARY KEY, name TEXT NOT NULL, value_kind TEXT NOT NULL
 CHECK(value_kind IN ('money','count','ratio'))''',
'payroll_metric': '''id TEXT PRIMARY KEY, institution_id TEXT NOT NULL REFERENCES institution(id),
 metric_code TEXT NOT NULL REFERENCES metric_definition(code), period_start TEXT NOT NULL,
 period_end TEXT NOT NULL CHECK(period_end>=period_start), period_kind TEXT NOT NULL
 CHECK(period_kind IN ('month','quarter','year','custom','as_of')),
 aggregation_kind TEXT NOT NULL CHECK(aggregation_kind IN ('period_total','ytd','snapshot','average')),
 scope_code TEXT NOT NULL, population_code TEXT NOT NULL,
 scenario TEXT NOT NULL CHECK(scenario IN ('actual','plan','estimate')),
 amount_minor INTEGER CHECK(amount_minor IS NULL OR (typeof(amount_minor)='integer' AND amount_minor>=0)),
 currency TEXT, count_10000 INTEGER CHECK(count_10000 IS NULL OR (typeof(count_10000)='integer' AND count_10000>=0)),
 ratio_100000000 INTEGER CHECK(ratio_100000000 IS NULL OR (typeof(ratio_100000000)='integer' AND ratio_100000000 BETWEEN 0 AND 100000000)),
 methodology_code TEXT NOT NULL, revision_no INTEGER NOT NULL CHECK(revision_no>=1),
 supersedes_id TEXT REFERENCES payroll_metric(id), is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
 verification_status TEXT NOT NULL CHECK(verification_status IN ('pending','verified','conflict','rejected')),
 CHECK((metric_code IN ('payroll_fund','payroll_via_sber') AND amount_minor IS NOT NULL
 AND currency IS NOT NULL AND length(currency)=3 AND count_10000 IS NULL AND ratio_100000000 IS NULL)
 OR (metric_code='employee_count' AND count_10000 IS NOT NULL AND amount_minor IS NULL AND currency IS NULL AND ratio_100000000 IS NULL)
 OR (metric_code='payroll_share' AND ratio_100000000 IS NOT NULL AND amount_minor IS NULL AND currency IS NULL AND count_10000 IS NULL))''',
'contract': '''id TEXT PRIMARY KEY, contract_type TEXT NOT NULL CHECK(contract_type IN ('contract','agreement','additional_agreement')),
 number_raw TEXT, number_normalized TEXT, signed_on TEXT, subject TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, status TEXT NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','active','completed','terminated','unknown')),
 amount_minor INTEGER CHECK(amount_minor IS NULL OR (typeof(amount_minor)='integer' AND amount_minor>=0)), currency TEXT,
 amount_status TEXT NOT NULL CHECK(amount_status IN ('known','unknown','not_applicable')),
 parent_contract_id TEXT REFERENCES contract(id), document_source_id TEXT REFERENCES source(id),
 verification_status TEXT NOT NULL DEFAULT 'pending' CHECK(verification_status IN ('pending','verified','conflict','rejected')),
 CHECK((amount_status='known' AND amount_minor IS NOT NULL AND currency IS NOT NULL AND length(currency)=3)
 OR (amount_status IN ('unknown','not_applicable') AND amount_minor IS NULL AND currency IS NULL)),
 CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to>valid_from),
 CHECK(parent_contract_id IS NULL OR parent_contract_id<>id)''',
'contract_party': '''id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES contract(id),
 legal_entity_id TEXT NOT NULL REFERENCES legal_entity(id), institution_id TEXT REFERENCES institution(id),
 party_role TEXT NOT NULL''',
'meeting': '''id TEXT PRIMARY KEY, title TEXT NOT NULL, starts_at TEXT, ends_at TEXT,
 date_precision TEXT NOT NULL DEFAULT 'unknown' CHECK(date_precision IN ('exact','day','month','unknown')),
 format TEXT NOT NULL DEFAULT 'unknown' CHECK(format IN ('offline','online','hybrid','unknown')),
 location TEXT, status TEXT NOT NULL DEFAULT 'unknown' CHECK(status IN ('planned','held','cancelled','unknown')),
 agenda TEXT, summary TEXT, organizer_user_id TEXT, external_system TEXT, external_event_id TEXT,
 minutes_source_id TEXT REFERENCES source(id), CHECK(ends_at IS NULL OR (starts_at IS NOT NULL AND ends_at>=starts_at))''',
'meeting_institution': '''meeting_id TEXT NOT NULL REFERENCES meeting(id), institution_id TEXT NOT NULL REFERENCES institution(id),
 PRIMARY KEY(meeting_id,institution_id)''',
'meeting_participant': '''id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meeting(id),
 person_id TEXT REFERENCES person(id), appointment_id TEXT REFERENCES appointment(id), user_id TEXT,
 display_name_snapshot TEXT NOT NULL, role_snapshot TEXT, institution_id TEXT REFERENCES institution(id),
 attendance_status TEXT NOT NULL DEFAULT 'unknown' CHECK(attendance_status IN ('invited','attended','absent','unknown'))''',
'follow_up': '''id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, institution_id TEXT NOT NULL,
 description TEXT NOT NULL, owner_user_id TEXT, due_on TEXT,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','done','cancelled')),
 completed_at TEXT, result TEXT, FOREIGN KEY(meeting_id,institution_id) REFERENCES meeting_institution(meeting_id,institution_id),
 CHECK(status<>'done' OR (completed_at IS NOT NULL AND result IS NOT NULL))''',
'source': '''id TEXT PRIMARY KEY, kind TEXT NOT NULL, uri TEXT NOT NULL, original_name TEXT,
 sha256 TEXT NOT NULL UNIQUE, source_date TEXT, collected_at TEXT NOT NULL,
 access_class TEXT NOT NULL DEFAULT 'internal' CHECK(access_class IN ('public','internal','restricted'))''',
'source_receipt': '''id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
 department_id TEXT NOT NULL REFERENCES department(id), original_name TEXT NOT NULL,
 received_at TEXT NOT NULL, UNIQUE(source_id,department_id,original_name)''',
'source_record': '''id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id), locator TEXT NOT NULL,
 raw_payload TEXT NOT NULL CHECK(json_valid(raw_payload)), raw_hash TEXT NOT NULL, UNIQUE(source_id,locator)''',
'import_batch': '''id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 mapping_version TEXT NOT NULL, rules_version TEXT NOT NULL, status TEXT NOT NULL,
 initiated_by TEXT NOT NULL, counters TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(counters))''',
'registry_candidate': '''id TEXT PRIMARY KEY, name TEXT NOT NULL, name_normalized TEXT NOT NULL,
 region_raw TEXT NOT NULL, education_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'needs_review',
 UNIQUE(name_normalized,region_raw)''',
'staging_record': '''id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batch(id),
 source_record_id TEXT NOT NULL REFERENCES source_record(id), candidate_id TEXT REFERENCES registry_candidate(id),
 row_type TEXT NOT NULL, normalized_payload TEXT NOT NULL CHECK(json_valid(normalized_payload)), candidate_inn TEXT,
 matched_institution_id TEXT REFERENCES institution(id), status TEXT NOT NULL CHECK(status IN ('ready','needs_review','rejected','applied')),
 errors TEXT NOT NULL CHECK(json_valid(errors)), UNIQUE(batch_id,source_record_id)''',
'entity_registry': '''id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, business_id TEXT NOT NULL,
 UNIQUE(entity_type,business_id)''',
'field_observation': '''id TEXT PRIMARY KEY, source_record_id TEXT NOT NULL REFERENCES source_record(id),
 field_path TEXT NOT NULL, raw_value TEXT NOT NULL, normalized_value TEXT, observed_at TEXT NOT NULL,
 effective_on TEXT, target_record_id TEXT REFERENCES entity_registry(id), target_field TEXT,
 review_status TEXT NOT NULL DEFAULT 'pending', reviewed_by TEXT, reviewed_at TEXT, decision_reason TEXT,
 accepted_entity_version INTEGER,
 UNIQUE(source_record_id,field_path,target_record_id,target_field)''',
'data_quality_issue': '''id TEXT PRIMARY KEY, staging_record_id TEXT REFERENCES staging_record(id),
 observation_id TEXT REFERENCES field_observation(id), rule_code TEXT NOT NULL, severity TEXT NOT NULL,
 description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', assigned_to TEXT, resolution TEXT, resolved_at TEXT,
 CHECK(staging_record_id IS NOT NULL OR observation_id IS NOT NULL), UNIQUE(staging_record_id,rule_code)''',
'change_log': '''id TEXT PRIMARY KEY, entity_registry_id TEXT NOT NULL REFERENCES entity_registry(id),
 field_name TEXT NOT NULL, old_value TEXT, new_value TEXT, changed_at TEXT NOT NULL, changed_by TEXT NOT NULL,
 reason TEXT NOT NULL, batch_id TEXT REFERENCES import_batch(id), observation_id TEXT REFERENCES field_observation(id),
 entity_version INTEGER NOT NULL''',
'import_application': '''id TEXT PRIMARY KEY, source_record_id TEXT NOT NULL REFERENCES source_record(id),
 mapping_version TEXT NOT NULL, rules_version TEXT NOT NULL, output_key TEXT NOT NULL,
 entity_registry_id TEXT NOT NULL REFERENCES entity_registry(id), applied_payload_hash TEXT NOT NULL,
 batch_id TEXT NOT NULL REFERENCES import_batch(id), UNIQUE(source_record_id,mapping_version,rules_version,output_key)''',
'identity_resolution': '''id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE REFERENCES registry_candidate(id),
 institution_id TEXT NOT NULL REFERENCES institution(id), evidence TEXT NOT NULL, verified_by TEXT NOT NULL,
 verified_at TEXT NOT NULL, resolution_source_id TEXT NOT NULL REFERENCES source(id)''',
}
BUSINESS = {'legal_entity','institution','institution_alias','contact_point','person','appointment',
 'contact_channel','payroll_metric','contract','contract_party','meeting','meeting_participant','follow_up'}

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db

def execute_statements(db, script):
    statement = ''
    for line in script.splitlines():
        statement += line + '\n'
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''
    if statement.strip():
        raise ValueError('Incomplete migration SQL')


def initialize(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (0,1):
        raise ValueError(f'Unsupported database version: {version}')
    if version == 1:
        return
    with db:
        db.execute('BEGIN')
        for name, fields in TABLES.items():
            if name in BUSINESS:
                fields = "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, created_by TEXT NOT NULL DEFAULT 'import', updated_by TEXT NOT NULL DEFAULT 'import', version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1), " + fields
            db.execute(f'CREATE TABLE {name} ({fields})')
        execute_statements(db,'''
        CREATE UNIQUE INDEX one_head ON institution(legal_entity_id) WHERE unit_type='head';
        CREATE UNIQUE INDEX external_alias ON institution_alias(source_system,external_id) WHERE external_id IS NOT NULL;
        CREATE UNIQUE INDEX channel_owner ON contact_channel(COALESCE(contact_point_id,''),COALESCE(appointment_id,''),channel_type,value_normalized,COALESCE(extension,''),COALESCE(valid_from,''),COALESCE(valid_to,''));
        CREATE UNIQUE INDEX current_payroll ON payroll_metric(institution_id,metric_code,period_start,period_end,period_kind,aggregation_kind,scope_code,population_code,scenario,methodology_code,COALESCE(currency,'')) WHERE is_current=1 AND verification_status='verified';
        CREATE UNIQUE INDEX contract_party_unique ON contract_party(contract_id,legal_entity_id,COALESCE(institution_id,''),party_role);
        CREATE UNIQUE INDEX meeting_external ON meeting(external_system,external_event_id) WHERE external_system IS NOT NULL AND external_event_id IS NOT NULL;
        CREATE INDEX alias_search ON institution_alias(alias_normalized);
        CREATE INDEX staging_status ON staging_record(batch_id,status);
        CREATE INDEX quality_status ON data_quality_issue(status);
        CREATE INDEX candidate_rows ON staging_record(candidate_id);
        CREATE INDEX meeting_date ON meeting(starts_at);
        CREATE INDEX contact_search ON contact_channel(channel_type,value_normalized);
        CREATE VIEW candidate_registry AS
          SELECT c.id,c.name,c.region_raw,c.education_type,c.status,
          COUNT(DISTINCT s.source_record_id) AS source_rows,
          COUNT(DISTINCT r.source_id) AS source_files,i.legal_entity_id,l.inn,i.unit_code
          FROM registry_candidate c LEFT JOIN staging_record s ON s.candidate_id=c.id
          LEFT JOIN source_record r ON r.id=s.source_record_id
          LEFT JOIN identity_resolution x ON x.candidate_id=c.id
          LEFT JOIN institution i ON i.id=x.institution_id LEFT JOIN legal_entity l ON l.id=i.legal_entity_id
          GROUP BY c.id;
        CREATE TRIGGER immutable_audit_update BEFORE UPDATE ON change_log BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
        CREATE TRIGGER immutable_audit_delete BEFORE DELETE ON change_log BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
        ''')
        for operation in ('INSERT','UPDATE'):
            db.execute(f'''CREATE TRIGGER institution_parent_{operation.lower()} BEFORE {operation} ON institution
            WHEN NEW.parent_institution_id IS NOT NULL BEGIN
              SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM institution p WHERE p.id=NEW.parent_institution_id
              AND p.legal_entity_id=NEW.legal_entity_id) THEN RAISE(ABORT,'parent legal entity mismatch') END;
              SELECT CASE WHEN EXISTS(WITH RECURSIVE parents(id,parent_id) AS
              (SELECT id,parent_institution_id FROM institution WHERE id=NEW.parent_institution_id
              UNION SELECT i.id,i.parent_institution_id FROM institution i JOIN parents p ON i.id=p.parent_id)
              SELECT 1 FROM parents WHERE id=NEW.id) THEN RAISE(ABORT,'institution cycle') END;
            END''')
            db.execute(f'''CREATE TRIGGER party_match_{operation.lower()} BEFORE {operation} ON contract_party
            WHEN NEW.institution_id IS NOT NULL BEGIN
              SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM institution WHERE id=NEW.institution_id
              AND legal_entity_id=NEW.legal_entity_id) THEN RAISE(ABORT,'contract party mismatch') END;
            END''')
        db.executemany('INSERT INTO metric_definition VALUES (?,?,?)',[
          ('payroll_fund','Фонд оплаты труда','money'),('payroll_via_sber','Перечисления через Сбер','money'),
          ('employee_count','Численность','count'),('payroll_share','Доля Сбера','ratio')])
        db.execute('PRAGMA user_version=1')
