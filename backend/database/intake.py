"""Lossless intake: ambiguous identities are never accepted automatically."""
import hashlib
import json
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from openpyxl import load_workbook

RULES = '1'
MAPPING = 'contacts-1'
NAMESPACE = uuid.UUID('753a3a8d-a78f-43af-8ebd-2d8df167a32b')
HEADERS = {
 'регион':'region', 'учебное заведение':'name', 'название вуза':'name', 'наименование':'name',
 'вуз/спо':'education_type', 'тип':'education_type', 'точка контакта':'contact_point',
 'должность':'contact_point','контактное лицо':'person','фио':'person',
 'эл. почта':'email','электронная почта':'email','email':'email',
 'телефон':'phone','адрес расположения':'address','адрес':'address','инн':'inn','кпп':'kpp',
}
EMAIL = re.compile(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+')


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(*parts):
    return str(uuid.uuid5(NAMESPACE, json.dumps(parts, ensure_ascii=False)))


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def clean(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value))).strip()


def normalized(value):
    return clean(value).casefold().replace('ё','е')


def inn_valid(value):
    value = clean(value)
    if not re.fullmatch(r'[0-9]{10}',value) or value == '0000000000':
        return False
    return sum(int(x)*w for x,w in zip(value[:9],(2,4,10,3,5,9,4,6,8))) % 11 % 10 == int(value[-1])


def kind(value):
    value = normalized(value)
    if value.startswith('спо'):
        return 'spo'
    if value in ('вуз','во'):
        return 'university'
    return 'unknown'


def issue(db, staging_id, code, description, severity='warning'):
    db.execute('INSERT OR IGNORE INTO data_quality_issue(id,staging_record_id,rule_code,severity,description) VALUES (?,?,?,?,?)',
               (uid(staging_id,code),staging_id,code,severity,description))


def find_header(sheet):
    for row in sheet.iter_rows(min_row=1,max_row=min(sheet.max_row,30)):
        columns = {}
        for c in row:
            key = HEADERS.get(normalized(c.value))
            if key:
                if key in columns:
                    raise ValueError(f'{sheet.title}: duplicate mapped header {key}')
                columns[key] = c.column
        if 'name' in columns and ('region' in columns or 'inn' in columns):
            return row[0].row, columns
    return None, {}


def register(db, table, business_id):
    key = uid('entity',table,business_id)
    db.execute('INSERT OR IGNORE INTO entity_registry VALUES (?,?,?)',(key,table,business_id))
    return key


def apply_output(db, batch_id, source_record_id, table, key, values):
    record = db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if record is None:
        fields = ['id',*values]
        db.execute(f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                   [key,*values.values()])
    else:
        for field,value in values.items():
            if value not in (None,'') and record[field] not in (None,'',value):
                raise ValueError(f'Conflicting {table}.{field}: explicit review required')
    registry_id = register(db,table,key)
    application = uid('application',source_record_id,MAPPING,RULES,table,key)
    already = db.execute('SELECT 1 FROM import_application WHERE id=?',(application,)).fetchone()
    if not already:
        db.execute('INSERT INTO import_application VALUES (?,?,?,?,?,?,?,?)',
                   (application,source_record_id,MAPPING,RULES,table+':'+key,registry_id,
                    hashlib.sha256(dump(values).encode()).hexdigest(),batch_id))
        for field,value in values.items():
            if value is None:
                continue
            observation = uid('observation',source_record_id,table,key,field)
            db.execute('''INSERT OR IGNORE INTO field_observation
            (id,source_record_id,field_path,raw_value,normalized_value,observed_at,target_record_id,target_field,review_status)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (observation,source_record_id,field,dump(value),dump(value),now(),registry_id,field,'pending'))
        if record is None:
            db.execute('''INSERT INTO change_log(id,entity_registry_id,field_name,new_value,changed_at,
            changed_by,reason,batch_id,entity_version) VALUES (?,?,?,?,?,?,?,?,?)''',
            (uid('created',table,key),registry_id,'__created__',dump(values),now(),'import',
             'Initial import; identity approved separately, contact contents pending',batch_id,1))
    return key


def apply_contacts(db, batch, source_record, institution_id, payload):
    title = payload.get('contact_point','')
    emails = EMAIL.findall(' '.join((title,payload.get('email',''))))
    title_without_email = clean(EMAIL.sub('',title).strip(' <>;,/'))
    if not title_without_email and not emails and not payload.get('phone') and not payload.get('person'):
        return
    title_without_email = title_without_email or 'Контакт из файла (уточнить назначение)'
    point_id = uid('point',institution_id,normalized(title_without_email))
    apply_output(db,batch,source_record,'contact_point',point_id,{
      'institution_id':institution_id,'title_raw':title_without_email,
      'title_normalized':normalized(title_without_email)})
    for email in sorted(set(emails)):
        local,domain = email.rsplit('@',1)
        value = local+'@'+domain.lower()
        apply_output(db,batch,source_record,'contact_channel',uid('email',point_id,value),{
         'contact_point_id':point_id,'channel_type':'email','value_raw':email,'value_normalized':value})
    # Raw phone/person values remain in staging until a separate reviewed normalization step.


def import_file(db, path, department='Не указан'):
    path = Path(path).resolve()
    if path.suffix.lower() not in ('.xlsx','.xlsm'):
        raise ValueError('Supported Excel formats: .xlsx and .xlsm; .xls must be converted explicitly')
    blob = path.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    workbook = load_workbook(BytesIO(blob),data_only=False,read_only=False)
    try:
        layouts = [(s,*find_header(s)) for s in workbook]
        if not any(columns for _,_,columns in layouts):
            raise ValueError(f'No supported contact headers found in {path.name}; nothing imported')
        source = uid('source',sha)
        batch = str(uuid.uuid4())
        counts = {'data_rows':0,'review':0,'applied':0,'service_rows':0,'unsupported_sheets':0}
        with db:
            department_id=uid('department',department)
            db.execute('INSERT OR IGNORE INTO department VALUES (?,?)',(department_id,department))
            db.execute('''INSERT OR IGNORE INTO source(id,kind,uri,original_name,sha256,collected_at)
              VALUES (?,?,?,?,?,?)''',(source,'excel',str(path),path.name,sha,now()))
            db.execute('INSERT OR IGNORE INTO source_receipt VALUES (?,?,?,?,?)',
                       (uid('receipt',source,department,path.name),source,department_id,path.name,now()))
            db.execute('''INSERT INTO import_batch(id,started_at,mapping_version,rules_version,status,initiated_by)
              VALUES (?,?,?,?,?,?)''',(batch,now(),MAPPING,RULES,'running','local-cli'))
            for sheet,header,columns in layouts:
                if not columns:
                    counts['unsupported_sheets']+=1
                for cells in sheet:
                    populated=[c for c in cells if c.value is not None]
                    if not populated:
                        continue
                    row_number=cells[0].row
                    raw={c.coordinate:{'value':c.value,'type':c.data_type,'format':c.number_format} for c in populated}
                    raw_json=dump(raw)
                    record=uid(source,sheet.title,row_number)
                    db.execute('INSERT OR IGNORE INTO source_record VALUES (?,?,?,?,?)',
                      (record,source,f'{sheet.title}!{row_number}',raw_json,hashlib.sha256(raw_json.encode()).hexdigest()))
                    payload={key:clean(sheet.cell(row_number,column).value) for key,column in columns.items()}
                    errors=[]
                    row_type='data' if header is not None and row_number>header else 'header'
                    if not columns:
                        row_type='unsupported'
                        errors.append('unsupported_sheet')
                    candidate=None
                    inn=payload.get('inn','')
                    if row_type=='data':
                        counts['data_rows']+=1
                        if not payload.get('name'):
                            errors.append('missing_institution_name')
                        else:
                            region=payload.get('region','')
                            candidate=uid('candidate',normalized(payload['name']),normalized(region))
                            db.execute('''INSERT OR IGNORE INTO registry_candidate
                            (id,name,name_normalized,region_raw,education_type) VALUES (?,?,?,?,?)''',
                            (candidate,payload['name'],normalized(payload['name']),normalized(region),kind(payload.get('education_type',''))))
                        errors.append('missing_inn' if not inn else ('identity_unverified' if inn_valid(inn) else 'invalid_inn'))
                        if any(c.data_type=='f' for c in populated):
                            errors.append('formula_requires_review')
                        if not payload.get('education_type'):
                            errors.append('missing_education_type')
                        if EMAIL.search(payload.get('contact_point','')):
                            errors.append('email_in_contact_point')
                        if normalized(payload.get('region',''))=='ирутская область':
                            errors.append('region_typo')
                    else:
                        counts['service_rows']+=1
                    stage=uid(batch,record)
                    status='needs_review' if row_type=='data' else 'rejected'
                    resolved=db.execute('SELECT institution_id FROM identity_resolution WHERE candidate_id=?',(candidate,)).fetchone()
                    # Spreadsheet INN may contradict the approved mapping: never ignore it.
                    if resolved and row_type=='data':
                        expected=db.execute('SELECT l.inn FROM institution i JOIN legal_entity l ON l.id=i.legal_entity_id WHERE i.id=?',resolved).fetchone()[0]
                        if inn and inn!=expected:
                            errors.append('inn_mapping_conflict')
                        if not any(x in errors for x in ('formula_requires_review','inn_mapping_conflict')):
                            try:
                                db.execute('SAVEPOINT apply_row')
                                apply_contacts(db,batch,record,resolved[0],payload)
                                db.execute('RELEASE apply_row')
                                status='applied'
                                errors=[x for x in errors if x not in ('missing_inn','identity_unverified')]
                            except ValueError:
                                db.execute('ROLLBACK TO apply_row')
                                db.execute('RELEASE apply_row')
                                errors.append('existing_value_conflict')
                    if row_type=='data' and (payload.get('person') or payload.get('phone') or (payload.get('email') and not EMAIL.search(payload['email']))):
                        errors.append('contact_details_require_review')
                    db.execute('''INSERT INTO staging_record VALUES (?,?,?,?,?,?,?,?,?,?)''',
                      (stage,batch,record,candidate,row_type,dump(payload),inn or None,
                       resolved[0] if resolved else None,status,dump(errors)))
                    for error in errors:
                        issue(db,stage,error,error)
                    if row_type=='data':
                        counts['applied' if status=='applied' else 'review']+=1
            db.execute('UPDATE import_batch SET status=?,finished_at=?,counters=? WHERE id=?',
                       ('completed_with_review' if counts['review'] or counts['unsupported_sheets'] else 'completed',now(),dump(counts),batch))
        return {'batch_id':batch,'source_id':source,**counts}
    finally:
        workbook.close()

