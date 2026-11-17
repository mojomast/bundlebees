"""Hermes read tool. No producer credential, ingestion or worker configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

# Installed plugin checkouts include contrib beside bundlebees_plugin. Wheel
# installs expose rizzstack_rails directly. No user-provided import path.
_vendor = Path(__file__).resolve().parents[1] / 'contrib'
if (_vendor/'rizzstack_rails').is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0,str(_vendor))

from rizzstack_rails import contracts
from rizzstack_rails.__main__ import configured_query
from rizzstack_rails.query import GROUPS, KINDS, QUERY_SCHEMA, QueryError, wire_json

NAME = 'bundlebees_rails'
SCHEMA = {
    'name':NAME,
    'description':'Read a bounded, scoped page of untrusted Rails observations and evidence IDs.',
    'parameters':{'type':'object','additionalProperties':False,
        'required':['schema','selector','format','after','limit','max_bytes','include','show_tokens'],
        'properties':{
            'schema':{'type':'string','const':QUERY_SCHEMA},
            'selector':{'type':'object','additionalProperties':False,'required':['kind','id'],
                        'properties':{'kind':{'enum':list(KINDS)},'id':{'type':'string','pattern':'^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$'}}},
            'format':{'enum':['json','markdown']}, 'after':{'type':['string','null'],'maxLength':1024},
            'limit':{'type':'integer','minimum':1,'maximum':500},
            'max_bytes':{'type':'integer','minimum':1,'maximum':32768},
            'include':{'type':'array','maxItems':4,'items':{'enum':list(GROUPS)}},
            'show_tokens':{'type':'boolean'},
        }},
}


def profile(ctx):
    if ctx is None:
        return os.environ.get('RIZZSTACK_RAILS_DB'),None
    # If Hermes has no configured ledger, do not fall back to another profile.
    try:
        return ctx.get_config('rails_db',None),ctx.get_config('rails_read_scope',None)
    except Exception:
        raise QueryError('source_unavailable',3) from None


def handle(args, ctx=None):
    try:
        if isinstance(args,str):
            if len(args)>4096:
                raise QueryError('too_large')
            try:
                args = json.loads(contracts.canonicalize_text(args))
            except (ValueError,RecursionError,contracts.RailsRefusal):
                raise QueryError('invalid_request') from None
        path,scope = profile(ctx)
        return wire_json(configured_query(args,path,allowed_scope=scope))
    except QueryError as exc:
        return wire_json(exc.as_dict())


def register_tools(ctx):
    def handler(args=None, **_hermes_context):
        return handle(args,ctx)
    ctx.register_tool(name=NAME,toolset='bundlebees',schema=SCHEMA,handler=handler,
                      description=SCHEMA['description'],emoji='🐝')
