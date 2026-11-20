"""Administrative PostgreSQL migration CLI; DSNs are read only from the environment."""
import argparse
import json
import os
import sys
from pathlib import Path
from .legacy import load_legacy


def write_json(path,value):
    path=Path(path)
    repo=Path(__file__).resolve().parents[3]
    if path.resolve().is_relative_to(repo):
        raise ValueError('Store real-data reports outside the repository')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description='PostgreSQL / Supabase schema and legacy data migration')
    parser.add_argument('--env-file',type=Path,help='Local file containing DATABASE_URL; never print it')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('migrate'); sub.add_parser('validate')
    for command in ('plan','seed'):
        cmd=sub.add_parser(command)
        cmd.add_argument('--repository',type=Path,default=Path(__file__).resolve().parents[3])
        cmd.add_argument('--input',action='append',type=Path,default=[])
        cmd.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.env_file:
            for line in args.env_file.read_text(encoding='utf-8-sig').splitlines():
                line=line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key,value=line.split('=',1)
                    if key.strip()=='DATABASE_URL':
                        os.environ['DATABASE_URL']=value.strip().strip('\"\'')
        if args.command=='plan':
            plan=load_legacy(args.repository,args.input)
            write_json(args.report,plan)
            print(json.dumps(plan['summary'],ensure_ascii=False,indent=2))
            return 0
        dsn=os.environ.get('DATABASE_URL','')
        if not dsn:
            raise ValueError('Set DATABASE_URL or pass --env-file with the connection string')
        from .db import connect,migrate
        from .transfer import import_plan,validate
        with connect(dsn) as conn:
            if args.command=='migrate':
                result={'applied_migrations':migrate(conn)}
            elif args.command=='seed':
                result=import_plan(conn,load_legacy(args.repository,args.input))
                write_json(args.report,result)
            else:
                result=validate(conn)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0
    except ValueError as exc:
        print('Error: '+str(exc),file=sys.stderr)
        return 1
    except Exception as exc:
        # Database exceptions may include a DSN or sensitive row values. Do not echo them.
        print('Database operation failed ('+type(exc).__name__+'). Inspect the local connection and validation report; credentials were not printed.',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
