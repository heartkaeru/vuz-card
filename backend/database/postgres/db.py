"""Checksummed, transaction-safe SQL migrations. No credentials in command output."""
import hashlib
from pathlib import Path
import psycopg
from psycopg.conninfo import conninfo_to_dict

MIGRATION_DIR=Path(__file__).resolve().parents[3]/'supabase'/'migrations'
# __file__ is backend/database/postgres/db.py: repository is parents[3].

def migration_checksums(data):
    normalized=data.replace(b'\r\n',b'\n').replace(b'\r',b'\n')
    crlf=normalized.replace(b'\n',b'\r\n')
    canonical=hashlib.sha256(normalized).hexdigest()
    return canonical, {
        canonical,
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(crlf).hexdigest(),
    }

def connect(dsn, *, application_name='vuz-card-migration', row_factory=None):
    try:
        options=conninfo_to_dict(dsn)
    except psycopg.Error as exc:
        raise ValueError('Invalid PostgreSQL connection string') from exc
    settings={'connect_timeout':15,'application_name':application_name,'prepare_threshold':None}
    if row_factory is not None:
        settings['row_factory']=row_factory
    if options.get('host') not in (None,'127.0.0.1','localhost','::1'):
        if options.get('sslmode') in ('disable','allow','prefer'):
            raise ValueError('Remote database connections require sslmode=require or certificate verification')
        if not options.get('sslmode'):
            settings['sslmode']='require'
    return psycopg.connect(dsn, **settings)


def migrate(conn, directory=MIGRATION_DIR):
    applied=[]
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(7262041120)')
        conn.execute('''CREATE TABLE IF NOT EXISTS public.vuz_schema_migrations(
          name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())''')
        conn.execute("REVOKE ALL ON public.vuz_schema_migrations FROM PUBLIC")
        for path in sorted(Path(directory).glob('*.sql')):
            data=path.read_bytes()
            checksum,checksums=migration_checksums(data)
            prior=conn.execute('SELECT sha256 FROM public.vuz_schema_migrations WHERE name=%s',(path.name,)).fetchone()
            if prior:
                if prior[0] not in checksums:
                    raise ValueError('Applied migration checksum changed: '+path.name)
                continue
            conn.execute(data.decode('utf-8-sig'),prepare=False)
            conn.execute('INSERT INTO public.vuz_schema_migrations(name,sha256) VALUES (%s,%s)',(path.name,checksum))
            applied.append(path.name)
    return applied


def set_actor(conn,kind='migration',label='first-iteration-import',actor_id=None):
    if kind not in ('human','ai','migration','system'):
        raise ValueError('Unknown actor kind')
    conn.execute("SELECT set_config('vuz.actor_type',%s,true),set_config('vuz.actor_label',%s,true),set_config('vuz.actor_id',%s,true)",
                 (kind,label,str(actor_id) if actor_id else ''))
