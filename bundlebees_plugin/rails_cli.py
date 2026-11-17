"""Read-only BundleBees CLI adapter. The runtime owns validation and rendering."""
from .rails_tool import profile
from rizzstack_rails.__main__ import SafeParser, add_query_parser, main, run_query
from rizzstack_rails.query import QueryError, wire_json
import sys


def add_parsers(subparsers):
    rails = subparsers.add_parser('rails',help='Query the configured Rails ledger.',allow_abbrev=False)
    add_query_parser(rails.add_subparsers(dest='rails_handler',required=True,parser_class=SafeParser))


def cmd_rails(ctx,args):
    try:
        path,scope = profile(ctx)
        return run_query(args,path,allowed_scope=scope)
    except QueryError as exc:
        print(wire_json(exc.as_dict()),file=sys.stderr)
        return exc.exit_code


if __name__ == '__main__':
    sys.exit(main())
