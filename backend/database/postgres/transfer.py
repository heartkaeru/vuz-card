"""Idempotent, transactional import. Conflicts abort rather than overwrite edits."""
from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4,UUID
import psycopg
import hashlib
import json
from psycopg import sql
from psycopg.types.json import Jsonb
from .db import set_actor
from .legacy import stable_id,name_key,payroll_amount


def equivalent(old,new):
    if isinstance(new,UUID):
        return str(old)==str(new)
    if isinstance(new,datetime) and isinstance(old,str):
        try:
            return datetime.fromisoformat(old.replace('Z','+00:00'))==new
        except ValueError:
            return False
    if isinstance(old,dict) and isinstance(new,dict):
        return old.keys()==new.keys() and all(equivalent(old[k],new[k]) for k in old)
    if isinstance(old,(list,tuple)) and isinstance(new,(list,tuple)):
        return len(old)==len(new) and all(equivalent(a,b) for a,b in zip(old,new))
    if isinstance(old,(int,float,Decimal)) and not isinstance(old,bool) and isinstance(new,(int,float,Decimal)) and not isinstance(new,bool):
        return Decimal(str(old))==Decimal(str(new))
    return old==new


def prefetch_existing(conn,plan):
    sources=[s['id'] for s in plan['sources']]
    universities=[u['id'] for u in plan['universities']]
    source_tables=('vuz_import_records','vuz_university_aliases','vuz_legacy_fields')
    university_tables=('vuz_officials','vuz_university_metrics','vuz_projects')
    queries=[]; params=[]
    specs=[('vuz_sources','id = ANY(%s::uuid[])',sources),
           ('vuz_universities','id = ANY(%s::uuid[])',universities)]
    specs += [(t,'source_id = ANY(%s::uuid[])',sources) for t in source_tables]
    specs += [(t,'university_id = ANY(%s::uuid[])',universities) for t in university_tables]
    specs.append(('vuz_official_contacts','official_id IN (SELECT id FROM public.vuz_officials WHERE university_id = ANY(%s::uuid[]))',universities))
    for table,condition,ids in specs:
        queries.append(sql.SQL("SELECT {}::text,coalesce(jsonb_agg(to_jsonb(t)),'[]'::jsonb)::text FROM public.{} t WHERE ").format(sql.Literal(table),sql.Identifier(table))+sql.SQL(condition))
        params.append(ids)
    cache={}
    for table,raw in conn.execute(sql.SQL(' UNION ALL ').join(queries),params):
        for row in json.loads(raw,parse_float=Decimal):
            cache[(table,str(row['id']))]=row
    return cache


def insert_or_compare(conn,table,values,ignore=(),cache=None):
    identity=(table,str(values['id']))
    existing=cache.get(identity) if cache is not None else None
    if existing is not None:
        for key,new in values.items():
            if key not in ignore and not equivalent(existing.get(key),new):
                raise ValueError(f'Existing data differs in {table}.{key}; no values overwritten')
        return False
    keys=list(values)
    query=sql.SQL('INSERT INTO public.{} ({}) VALUES ({}) ON CONFLICT(id) DO NOTHING RETURNING id').format(
        sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,keys)),sql.SQL(',').join(sql.Placeholder() for _ in keys))
    params=[Jsonb(v) if isinstance(v,(dict,list)) or key in ('payload','summary') or (table=='vuz_legacy_fields' and key=='value') else v for key,v in values.items()]
    created=conn.execute(query,params).fetchone()
    if created:
        if cache is not None:
            cache[identity]=dict(values)
        return True
    row=conn.execute(sql.SQL('SELECT {} FROM public.{} WHERE id=%s').format(
        sql.SQL(',').join(map(sql.Identifier,keys)),sql.Identifier(table)),(values['id'],)).fetchone()
    if row is None:
        raise ValueError('Conflicting natural key: '+table)
    for key,old,new in zip(keys,row,values.values()):
        if key not in ignore and not equivalent(old,new):
            raise ValueError(f'Existing data differs in {table}.{key}; no values overwritten')
    if cache is not None:
        cache[identity]=dict(zip(keys,row))
    return False


def _time(raw):
    if not raw:
        return None
    try:
        value=datetime.fromisoformat(raw.replace('Z','+00:00'))
        return value if value.tzinfo else None
    except (ValueError,AttributeError):
        return None


def import_plan(conn,plan,actor_label='first-iteration-import'):
    result={**plan['summary'],'inserted':{},'batch_id':str(uuid4())}
    batch_id=result['batch_id']
    cache={}
    def write(table,values,ignore=()):
        added=insert_or_compare(conn,table,values,ignore,cache)
        result['inserted'][table]=result['inserted'].get(table,0)+int(added)
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(7262041121)')
        set_actor(conn,'migration',actor_label)
        cache.update(prefetch_existing(conn,plan))
        conn.execute("INSERT INTO public.vuz_import_batches(id,status,importer_version) VALUES (%s,'running','1')",(batch_id,))
        for source in plan['sources']:
            write('vuz_sources',source)
        for row in plan['universities']:
            write('vuz_universities',{
                'id':row['id'],'name':row['name'],'short_name':row.get('short_name'),
                'region':row.get('region'),'city':row.get('city'),'website':row.get('website'),
                'reported_inn':row.get('inn'),'is_demo':True,'verification_status':'needs_review'})
        for record in plan['records']:
            source_id=record['source_id']; source_key=record['source_key']; uni=record['university_id']; raw=record['payload']
            write('vuz_import_records',{'id':stable_id('record',source_id,source_key),'batch_id':batch_id,
              'source_id':source_id,'source_key':source_key,'university_id':uni,'payload':raw,
              'payload_hash':hashlib.sha256(json.dumps(raw,ensure_ascii=False,sort_keys=True).encode()).hexdigest()},ignore=('batch_id',))
            for alias in {raw['name'],raw.get('short_name')} - {None,''}:
                write('vuz_university_aliases',{'id':stable_id('alias',source_id,name_key(alias),uni),
                    'source_id':source_id,'university_id':uni,'alias':alias})
            # Preserve every input field, even those without a normalized mapping.
            for field,value in raw.items():
                write('vuz_legacy_fields',{'id':stable_id('legacy',source_id,source_key,field),
                  'source_id':source_id,'source_key':source_key,'field_name':field,'university_id':uni,
                  'value':value,'reason':'First iteration demo; source claim is not independently verified'})
            for index,official in enumerate(raw.get('officials',[])):
                if not isinstance(official,dict) or not official.get('fio') or not official.get('position'):
                    raise ValueError('Official requires full name and position')
                external=str(official.get('id',index))
                official_id=stable_id('official',uni,name_key(official['fio']),name_key(official['position']),name_key(official.get('department') or ''))
                fields={'id':official_id,'university_id':uni,'full_name':official['fio'],'position':official['position'],
                  'department':official.get('department'),'responsibilities':official.get('responsibilities'),
                  'biography':official.get('biography'),'photo_url':official.get('photo_url'),'source_url':official.get('source_url'),
                  'is_demo':True,'source_id':source_id,'external_key':source_key+':official:'+external,
                  'verification_status':'needs_review'}
                write('vuz_officials',fields,ignore=('source_id','external_key'))
                for field,channel in [('email','email'),('phone','phone')]:
                    value=official.get(field)
                    if not value:
                        continue
                    value=str(value).strip()
                    canonical=value
                    if channel=='email' and '@' in value:
                        local,domain=value.rsplit('@',1)
                        canonical=local+'@'+domain.lower()
                    write('vuz_official_contacts',{'id':stable_id('channel',official_id,channel,canonical),
                      'official_id':official_id,'channel_type':channel,'value_raw':value,'value_normalized':canonical,
                      'source_id':source_id,'verification_status':'needs_review'},ignore=('source_id',))
            for field,code,unit in [('fot_volume','payroll_fund','RUB'),('contracts_count','active_contracts_reported','contract')]:
                raw_value=raw.get(field)
                if raw_value is None:
                    continue
                if field=='fot_volume':
                    value=payroll_amount(raw_value)
                else:
                    value=Decimal(raw_value) if isinstance(raw_value,int) and not isinstance(raw_value,bool) and raw_value>=0 else None
                if value is None:
                    continue  # Unparsed raw value remains visible in vuz_legacy_fields.
                external=source_key+':'+field
                write('vuz_university_metrics',{'id':stable_id('metric',uni,code),
                  'university_id':uni,'metric_code':code,'value':value,'unit':unit,'raw_value':str(raw_value),
                  'source_as_of':_time(raw.get('updated_at')),'source_id':source_id,'external_key':external,
                  'is_demo':True,'verification_status':'needs_review'},ignore=('source_id','external_key'))
            for title in raw.get('active_projects',[]):
                if not isinstance(title,str) or not title.strip():
                    raise ValueError('Project names must be nonempty strings')
                write('vuz_projects',{'id':stable_id('project',uni,name_key(title)),
                  'university_id':uni,'name':title,'source_id':source_id,'external_key':source_key+':project:'+name_key(title),
                  'kind':'university','partnership_status':'reported','sber_confirmed':False,
                  'is_demo':True,'verification_status':'needs_review'},ignore=('source_id','external_key'))
        result['validation']=validate(conn)
        if result['validation']['duplicate_names'] or result['validation']['orphan_officials']:
            raise ValueError('Post-import validation failed')
        conn.execute("UPDATE public.vuz_import_batches SET status='completed',finished_at=now(),summary=%s WHERE id=%s",(Jsonb(result),batch_id))
    return result


def validate(conn):
    result={}
    for table in ('vuz_universities','vuz_officials','vuz_official_contacts','vuz_university_metrics',
                  'vuz_projects','vuz_admission_campaigns','vuz_infrastructure','vuz_product_recommendations',
                  'vuz_contracts','vuz_meetings','vuz_sources','vuz_import_records','vuz_legacy_fields','vuz_audit_log'):
        result[table]=conn.execute(sql.SQL('SELECT count(*) FROM public.{}').format(sql.Identifier(table))).fetchone()[0]
    result['duplicate_names']=conn.execute('SELECT count(*) FROM (SELECT name_key FROM public.vuz_universities GROUP BY name_key HAVING count(*)>1) d').fetchone()[0]
    result['orphan_officials']=conn.execute('SELECT count(*) FROM public.vuz_officials o LEFT JOIN public.vuz_universities u ON u.id=o.university_id WHERE u.id IS NULL').fetchone()[0]
    result['unvalidated_constraints']=conn.execute("SELECT count(*) FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_class t ON t.oid=c.conrelid JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND left(t.relname,4)='vuz_' AND NOT c.convalidated").fetchone()[0]
    result['demo_universities']=conn.execute('SELECT count(*) FROM public.vuz_universities WHERE is_demo').fetchone()[0]
    return result
