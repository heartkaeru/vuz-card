"""Reviewed identity mappings and reproducible local reports."""
import csv
import json
import sqlite3
from pathlib import Path
from .intake import clean, normalized, uid, now, dump, inn_valid, register


def load_identities(db, path):
    path=Path(path).resolve()
    blob=path.read_bytes()
    import hashlib
    source_id=uid('source',hashlib.sha256(blob).hexdigest())
    entries=json.loads(blob.decode('utf-8-sig'))
    if not isinstance(entries,list):
        raise ValueError('Identity mapping must be a JSON array')
    required=('source_name','source_region','inn','official_name','unit_code','unit_type',
              'verified_by','verified_at','evidence')
    # Validate all entries before applying any of them.
    seen=set()
    for entry in entries:
        if not isinstance(entry,dict) or any(not clean(entry.get(x)) for x in required):
            raise ValueError('Every identity requires: '+', '.join(required))
        if not inn_valid(entry['inn']):
            raise ValueError('Invalid INN in identity mapping')
        if entry['unit_type'] not in ('head','branch','college','other'):
            raise ValueError('Invalid unit_type')
        from datetime import datetime
        stamp=datetime.fromisoformat(entry['verified_at'])
        if stamp.tzinfo is None:
            raise ValueError('verified_at must include a timezone offset')
        identity=(normalized(entry['source_name']),normalized(entry['source_region']))
        if identity in seen:
            raise ValueError('Duplicate identity mapping')
        seen.add(identity)
        if not db.execute('SELECT 1 FROM registry_candidate WHERE name_normalized=? AND region_raw=?',identity).fetchone():
            raise ValueError('Unknown candidate; import the contact source first')
    with db:
        db.execute('''INSERT OR IGNORE INTO source(id,kind,uri,original_name,sha256,collected_at,access_class)
         VALUES (?,?,?,?,?,?,?)''',(source_id,'manual',str(path),path.name,hashlib.sha256(blob).hexdigest(),now(),'internal'))
        for index,entry in enumerate(entries):
            name,region=normalized(entry['source_name']),normalized(entry['source_region'])
            candidate=db.execute('SELECT id FROM registry_candidate WHERE name_normalized=? AND region_raw=?',(name,region)).fetchone()[0]
            inn=clean(entry['inn'])
            legal=uid('legal',inn)
            institution=uid('institution',inn,entry['unit_code'])
            existing_resolution=db.execute('SELECT institution_id FROM identity_resolution WHERE candidate_id=?',(candidate,)).fetchone()
            if existing_resolution and existing_resolution[0]!=institution:
                raise ValueError('Candidate already resolved differently; explicit correction workflow required')
            record=uid(source_id,index)
            db.execute('INSERT OR IGNORE INTO source_record VALUES (?,?,?,?,?)',
              (record,source_id,f'identity[{index}]',dump(entry),hashlib.sha256(dump(entry).encode()).hexdigest()))
            old=db.execute('SELECT official_name FROM legal_entity WHERE id=?',(legal,)).fetchone()
            if old and old[0]!=entry['official_name']:
                raise ValueError('Conflicting official names for INN')
            db.execute('''INSERT OR IGNORE INTO legal_entity(id,inn,official_name,verified_at,created_by,updated_by)
             VALUES (?,?,?,?,?,?)''',(legal,inn,entry['official_name'],entry['verified_at'],entry['verified_by'],entry['verified_by']))
            old_i=db.execute('SELECT unit_type FROM institution WHERE id=?',(institution,)).fetchone()
            if old_i and old_i[0]!=entry['unit_type']:
                raise ValueError('Conflicting institution unit_type')
            db.execute('''INSERT OR IGNORE INTO institution(id,legal_entity_id,unit_code,display_name,education_type,
             unit_type,kpp,created_by,updated_by) VALUES (?,?,?,?,?,?,?,?,?)''',
             (institution,legal,entry['unit_code'],entry.get('display_name',entry['source_name']),
              entry.get('education_type','unknown'),entry['unit_type'],entry.get('kpp'),entry['verified_by'],entry['verified_by']))
            db.execute('''INSERT OR IGNORE INTO institution_alias(id,institution_id,alias_raw,alias_normalized,source_system)
             VALUES (?,?,?,?,?)''',(uid('alias',institution,name),institution,entry['source_name'],name,'excel'))
            db.execute('INSERT OR IGNORE INTO identity_resolution VALUES (?,?,?,?,?,?,?)',
              (uid('resolution',candidate),candidate,institution,entry['evidence'],entry['verified_by'],entry['verified_at'],source_id))
            db.execute("UPDATE registry_candidate SET status='identity_verified' WHERE id=?",(candidate,))
            for table,key,fields in [('legal_entity',legal,{'inn':inn,'official_name':entry['official_name']}),
                                     ('institution',institution,{'unit_code':entry['unit_code'],'unit_type':entry['unit_type']})]:
                entity=register(db,table,key)
                for field,value in fields.items():
                    observation=uid('resolution-observation',source_id,index,entity,field)
                    db.execute('''INSERT OR IGNORE INTO field_observation
                    (id,source_record_id,field_path,raw_value,normalized_value,observed_at,target_record_id,target_field,
                     review_status,reviewed_by,reviewed_at,decision_reason,accepted_entity_version)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (observation,record,field,dump(value),dump(value),now(),entity,field,'verified',entry['verified_by'],
                     entry['verified_at'],entry['evidence'],1))
                    db.execute('''INSERT OR IGNORE INTO change_log(id,entity_registry_id,field_name,new_value,changed_at,
                    changed_by,reason,observation_id,entity_version) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (uid('resolution-log',observation),entity,field,dump(value),now(),entry['verified_by'],entry['evidence'],observation,1))
    return len(entries)


def summary(db):
    tables=('legal_entity','institution','registry_candidate','source','source_record','staging_record',
            'contact_point','person','appointment','contact_channel','payroll_metric','contract','meeting','import_batch')
    result={t:db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
    result['integrity']=db.execute('PRAGMA integrity_check').fetchone()[0]
    result['foreign_key_errors']=[list(r) for r in db.execute('PRAGMA foreign_key_check')]
    result['latest_batches']=[dict(r) for r in db.execute('SELECT * FROM import_batch ORDER BY rowid DESC LIMIT 10')]
    result['open_issues']=[dict(r) for r in db.execute("""WITH latest AS (
      SELECT s.id,ROW_NUMBER() OVER (PARTITION BY source_record_id ORDER BY s.rowid DESC) rn FROM staging_record s)
      SELECT q.rule_code,COUNT(*) AS count FROM data_quality_issue q JOIN latest l ON l.id=q.staging_record_id
      WHERE q.status='open' AND l.rn=1 GROUP BY q.rule_code""")]
    return result


def write_report(db, directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    result=summary(db)
    (directory/'import_report.json').write_text(dump(result)+'\n',encoding='utf-8')
    queries={
      'registry_candidates.csv':'SELECT * FROM candidate_registry ORDER BY region_raw,name',
      'accepted_registry.csv':'''SELECT i.id,l.inn,l.official_name,i.display_name,i.unit_code,i.unit_type
          FROM institution i JOIN legal_entity l ON l.id=i.legal_entity_id ORDER BY l.inn,i.unit_code''',
      'review_queue.csv':'''SELECT s.id,b.started_at,r.locator,o.original_name,c.name,s.normalized_payload,s.errors,s.status
          FROM staging_record s JOIN source_record r ON r.id=s.source_record_id JOIN source o ON o.id=r.source_id
          JOIN import_batch b ON b.id=s.batch_id LEFT JOIN registry_candidate c ON c.id=s.candidate_id
          WHERE s.rowid=(SELECT MAX(s2.rowid) FROM staging_record s2 WHERE s2.source_record_id=s.source_record_id) AND s.status='needs_review' ''',
    }
    for filename,query in queries.items():
        cursor=db.execute(query)
        with (directory/filename).open('w',newline='',encoding='utf-8-sig') as stream:
            writer=csv.writer(stream,delimiter=';')
            writer.writerow([c[0] for c in cursor.description])
            # Prevent spreadsheet formula execution on opening the exports.
            for row in cursor:
                writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
    excel_count=db.execute("SELECT COUNT(*) FROM source WHERE kind='excel'").fetchone()[0]
    latest=result['latest_batches'][0] if result['latest_batches'] else None
    counts=json.loads(latest['counters']) if latest else {}
    text=['# Результат первичной загрузки','',f'Сформировано (UTC): {now()}',
          '',f"- Исходных файлов Excel: {excel_count}.",
          f"- Уникальных строк исходников: {result['source_record']} (включая заголовки).",
          f"- Групп названий для проверки: {result['registry_candidate']}.",
          f"- Подтверждённых юридических лиц: {result['legal_entity']}.",
          f"- Подтверждённых карточек учреждений: {result['institution']}.",
          f"- Последний импорт: {dump(counts)}.",
          f"- Целостность: {result['integrity']}; нарушений FK: {len(result['foreign_key_errors'])}.",
          '', 'Кандидаты сгруппированы только по точному нормализованному названию и региону. Это не подтверждённое объединение по ИНН.',
          'Строки без названия не привязаны к предыдущему учреждению автоматически. Исходные значения сохранены.',
          'ФОТ, договоры и встречи не заполнены при отсутствии предоставленных данных.',
          '', '## Файлы', '', '- registry_candidates.csv — предварительный реестр названий.',
          '- accepted_registry.csv — только подтверждённые карточки.',
          '- review_queue.csv — актуальная попытка каждой исходной строки, требующей проверки.',
          '- import_report.json — числа, статусы партий и причины проверки.',
          '', '## Следующий шаг', '',
          'Заполнить identity mapping по подтверждённым реквизитам, выполнить resolve, повторить import. ',
          'Подтверждение личности учреждения не подтверждает актуальность контактов: они загружаются со статусом pending.']
    (directory/'IMPORT_REPORT.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    return result
