"""Run from the repository: python -m backend.database --help."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
from .schema import connect, initialize
from .intake import import_file
from .review import load_identities, summary, write_report


def main():
    parser=argparse.ArgumentParser(description='Локальная БД ВУЗ-CRM и первичный импорт Excel')
    parser.add_argument('--db',type=Path,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init')
    importer=commands.add_parser('import')
    importer.add_argument('files',nargs='+',type=Path)
    importer.add_argument('--department',default='Не указан')
    resolver=commands.add_parser('resolve')
    resolver.add_argument('mapping',type=Path)
    reporter=commands.add_parser('report')
    reporter.add_argument('--output',type=Path,required=True)
    commands.add_parser('status')
    args=parser.parse_args()
    # Keep actual data out of the tracked project by default and by construction.
    repo=Path(__file__).resolve().parents[2]
    if args.db.resolve().is_relative_to(repo):
        parser.error('Database must be outside the repository (e.g. ../data_work/vuz_card.sqlite3)')
    if args.command=='report' and args.output.resolve().is_relative_to(repo):
        parser.error('Reports with actual data must be outside the repository')
    try:
        with connect(args.db) as db:
            initialize(db)
            if args.command=='import':
                result=[import_file(db,p,args.department) for p in args.files]
            elif args.command=='resolve':
                result={'resolved':load_identities(db,args.mapping)}
            elif args.command=='report':
                result=write_report(db,args.output)
            else:
                result=summary(db)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError,OSError,sqlite3.Error) as exc:
        print(f'Ошибка: {exc}',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
