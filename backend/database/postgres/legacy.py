"""Read literal first-iteration files without importing or executing their Python."""
import ast
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID,uuid5

NAMESPACE=UUID('bf4551cd-fcba-4d9f-a859-e223c4ceef98')


def stable_id(*parts):
    return uuid5(NAMESPACE,json.dumps(parts,ensure_ascii=False,sort_keys=True,default=str))


def name_key(value):
    value=re.sub(r'\s+',' ',str(value)).strip().casefold().replace('ё','е')
    return re.sub('[«»“”"„]','',value)


def literal_assignment(path,variable):
    tree=ast.parse(Path(path).read_text(encoding='utf-8-sig'),filename=str(path))
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==variable for t in node.targets):
            return ast.literal_eval(node.value)
        if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id==variable:
            return ast.literal_eval(node.value)
    raise ValueError(f'Missing literal {variable} in {Path(path).name}')


def payroll_amount(raw):
    """Parse a reported RUB amount; never guess a reporting period or a methodology."""
    match=re.fullmatch(r'\s*([0-9]+(?:[.,][0-9]+)?)\s*(млрд|млн|тыс)?\.?\s*(?:руб\.?|₽)\s*',str(raw),re.I)
    if not match:
        return None
    scale={None:Decimal(1),'тыс':Decimal(1000),'млн':Decimal(1000000),'млрд':Decimal(1000000000)}
    return Decimal(match[1].replace(',','.'))*scale[match[2].lower() if match[2] else None]


def load_legacy(repository, json_files=()):
    repository=Path(repository).resolve()
    inputs=[]
    for relative,variable,kind in [('backend/seed.py','UNIVERSITIES','seed'),
                                   ('backend/app/data/mock_universities.py','MOCK_UNIVERSITIES_DB','mock')]:
        path=repository/relative
        if path.exists():
            rows=literal_assignment(path,variable)
            if kind=='seed':
                rows=[{'name':n,'city':c,'website':w} for n,c,w in rows]
            elif isinstance(rows,dict):
                rows=list(rows.values())
            inputs.append((relative,path.read_bytes(),kind,rows))
    for path in json_files:
        path=Path(path).resolve()
        rows=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(rows,list):
            raise ValueError('JSON input must contain an array of university cards')
        inputs.append((path.name,path.read_bytes(),'json',rows))
    if not inputs:
        raise ValueError('No first-iteration source files found')
    universities={}
    aliases={}
    sources=[]
    records=[]
    for source_path,blob,kind,rows in inputs:
        sha=hashlib.sha256(blob).hexdigest()
        source_id=stable_id('source',source_path,sha)
        sources.append({'id':source_id,'source_path':source_path,'sha256':sha,'source_kind':kind,'is_demo':True})
        if not isinstance(rows,list):
            raise ValueError('Expected a literal list of university records')
        for index,row in enumerate(rows):
            if not isinstance(row,dict) or not isinstance(row.get('name'),str) or not name_key(row['name']):
                raise ValueError('Every input university must have a nonempty name')
            key=name_key(row['name'])
            existing=universities.get(key)
            if existing:
                # Exact-name deduplication is allowed only when filled identity fields agree.
                for field in ('city','website','inn'):
                    old,new=existing.get(field),row.get(field)
                    if old and new and str(old).strip().rstrip('/')!=str(new).strip().rstrip('/'):
                        raise ValueError(f'Conflicting {field} for duplicated university name: {row["name"]}')
            else:
                existing={'id':stable_id('university',key),'name':row['name'].strip()}
                universities[key]=existing
            for field in ('city','website','region','short_name','inn'):
                value=row.get(field)
                if value not in (None,''):
                    existing.setdefault(field,value)
            records.append({'source_id':source_id,'source_key':str(row.get('id',index)),
                            'university_id':existing['id'],'source_kind':kind,'payload':row})
    return {'sources':sources,'universities':list(universities.values()),'records':records,
            'summary':{'sources':len(sources),'input_records':len(records),'universities':len(universities),
                       'exact_name_duplicates_merged':len(records)-len(universities),
                       'demo_data':True,'verification_status':'needs_review'}}
