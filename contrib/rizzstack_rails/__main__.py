"""python -m rizzstack_rails query --kind attempt --id att_example"""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys

from . import RailsLedger, contracts
from .query import GROUPS, KINDS, QueryError, make_request, query_events, validate_request, wire_json


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes the rejected value (which could be a secret).
        self.exit(2, wire_json(QueryError('invalid_request').as_dict()) + '\n')


def add_query_parser(subparsers):
    p = subparsers.add_parser('query', help='Read a bounded page of Rails observations.',allow_abbrev=False)
    p.add_argument('--kind',choices=KINDS,required=True)
    p.add_argument('--id',required=True)
    p.add_argument('--format',choices=('json','markdown'),default='markdown')
    p.add_argument('--after')
    p.add_argument('--limit',type=int,default=100)
    p.add_argument('--max-bytes',type=int,default=32768)
    p.add_argument('--include',default=','.join(GROUPS),help='Comma-separated groups; empty excludes optional groups.')
    p.add_argument('--show-tokens',action='store_true')


def configured_query(request, path, *, allowed_scope=None):
    """path is administrator-owned profile configuration, never a wire field."""
    validate_request(request)
    if not isinstance(path,(str,Path)) or not Path(path).is_absolute() or str(path).startswith(('//','\\\\')):
        raise QueryError('source_unavailable',3)
    try:
        with closing(RailsLedger(path,read_only=True)) as reader:
            return query_events(request,reader,allowed_scope=allowed_scope)
    except (contracts.RailsRefusal,sqlite3.Error,OSError,ValueError):
        raise QueryError('source_unavailable',3) from None


def run_query(args, path, *, allowed_scope=None):
    try:
        req = make_request(args.kind,args.id,format=args.format,after=args.after,limit=args.limit,
                           max_bytes=args.max_bytes,include=args.include.split(',') if args.include else [],
                           show_tokens=args.show_tokens)
        result = configured_query(req,path,allowed_scope=allowed_scope)
        # Both formats carry the envelope so a model never loses pagination,
        # provenance or partial status when choosing a Markdown rendering.
        sys.stdout.write(wire_json(result))
        return 0
    except QueryError as exc:
        print(wire_json(exc.as_dict()),file=sys.stderr)
        return exc.exit_code


def main(argv=None):
    parser = SafeParser(prog='rizzstack_rails',allow_abbrev=False)
    add_query_parser(parser.add_subparsers(dest='command',required=True))
    try:
        args = parser.parse_args(argv)
        return run_query(args,os.environ.get('RIZZSTACK_RAILS_DB'))
    except QueryError as exc:
        print(wire_json(exc.as_dict()),file=sys.stderr)
        return exc.exit_code


if __name__ == '__main__':
    sys.exit(main())
