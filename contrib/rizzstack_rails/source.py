"""Read-only Rails source protocol and bounded, generation-aware JSONL framing.

Sources are configured in-process. A trusted projector selects safe structured
facts; this module never copies a raw JSONL object to the ledger by default.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Protocol

from . import contracts, privacy

MAX_RECORDS = 500
MAX_BYTES = 1 << 20
MAX_RECORD = 1 << 16
TOKEN = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,191}')
RESULT = 'rizzstack.rails-read-batch-result/1'


class SourceError(contracts.RailsRefusal):
    """Safe to report: never includes source values, file names or exceptions."""

    def __init__(self, code):
        super().__init__(code, code=code)


class Source(Protocol):
    def describe(self) -> dict: ...
    def read_batch(self, cursor: str | None, limit: int, byte_limit: int) -> dict: ...
    def health(self) -> dict: ...


def token(value):
    if not isinstance(value, str) or not TOKEN.fullmatch(value) or privacy.scrub_text(value) != value:
        raise SourceError('invalid_identifier')
    return value


def limits(limit, byte_limit):
    if (type(limit) is not int or not 1 <= limit <= MAX_RECORDS or
            type(byte_limit) is not int or not 1 <= byte_limit <= MAX_BYTES):
        raise SourceError('invalid_limits')


def digest(value):
    """Digest of *public, normalized* identity or semantics only."""
    return hashlib.sha256(contracts.canonicalize(value)).hexdigest()


def result(records, cursor, more, head=None, diagnostics=()):
    return dict(schema=RESULT, records=records, next_cursor=cursor, has_more=more,
                source_head=head, diagnostics=list(diagnostics))


def fit_page(read, limit, cap):
    """Include cursor, diagnostics and framing in the byte budget. Retry only
    against the same open file/snapshot when a boundary record doesn't fit.
    """
    while True:
        page = read(limit)
        if len(contracts.canonicalize(page)) <= cap:
            return page
        count = len(page['records'])
        if count <= 1:
            raise SourceError('byte_limit')
        limit = count // 2


class JsonlSource:
    """`generation` is a trusted public producer run ID, required even on POSIX.

    Change it on in-place rewrite/rotation (truncate-and-regrow between polls
    is not distinguishable from append by stat). An observed replacement or
    shrink also starts a new generation and emits a gap. The projector gets
    (row, {generation, offset}); it must use those coordinates or native fact
    IDs for stable keys. A partial/oversized unframed line is never skipped.
    """
    def __init__(self, path, *, adapter_id, source_instance, generation, project):
        self.path = Path(path)
        self.adapter = token(adapter_id)
        self.instance = token(source_instance)
        self.generation = token(generation)
        self.project = project
        self._last = None
        self._error = 'never_read'

    def describe(self):
        return dict(schema='rizzstack.rails-describe/1', adapter_id=self.adapter,
                    version='1', source_instance=self.instance,
                    capabilities=['generation-offset'], supported_formats=['jsonl'])

    def health(self):
        return dict(schema='rizzstack.rails-source-health/1',
                    state=('stale' if self._last else 'unavailable') if self._error else 'live',
                    last_success=self._last, error_code=self._error)

    def _notice(self, kind, code, gen, start, end):
        mark = digest([self.adapter, self.instance, gen, start, end, code])
        return dict(kind=kind, source=dict(adapter=self.adapter, instance=self.instance,
                                            record_id='frame:' + mark, revision=gen),
                    scope={'session_id': self.instance}, authority='imported_observation',
                    dedupe_key='frame:' + mark,
                    payload=dict(range=[start, end], cursor=end, count=1, code=code, digest=mark))

    def read_batch(self, cursor=None, limit=100, byte_limit=MAX_BYTES):
        limits(limit, byte_limit)
        if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(
                r'j1:[0-9a-f]{64}:\d{1,16}:\d{1,16}', cursor)):
            raise SourceError('invalid_cursor')
        try:
            with self.path.open('rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SourceError('source_unavailable')
                page = fit_page(lambda n: self._read(stream, info, cursor, n, byte_limit), limit, byte_limit)
        except OSError:
            self._error = 'source_unavailable'
            raise SourceError(self._error) from None
        except SourceError as exc:
            self._error = exc.code
            raise
        self._last, self._error = contracts.utc_now(), None
        return page

    def _read(self, stream, info, cursor, limit, byte_limit):
        stamp = digest([self.adapter, self.instance, self.generation, str(info.st_dev), str(info.st_ino)])
        epoch = offset = used = scanned = 0
        rows, diagnostics = [], []
        if cursor is not None:
            _, prior, epoch_text, offset_text = cursor.split(':')
            offset, epoch = int(offset_text), int(epoch_text)
            if offset > info.st_size or prior != stamp:
                epoch += 1
                gap = self._notice('source.gap', 'generation_changed', digest([stamp, epoch]), 0, offset)
                used = len(contracts.canonicalize(gap))
                if used > byte_limit:
                    raise SourceError('byte_limit')
                rows.append(gap)
                offset = 0
        stream.seek(offset)
        gen = digest([stamp, epoch])
        more = False
        while len(rows) < limit and offset < info.st_size:
            budget = byte_limit - scanned
            if budget <= 0:
                more = True
                break
            line = stream.readline(min(MAX_RECORD + 1, budget, info.st_size - offset))
            scanned += len(line)
            if len(line) > MAX_RECORD:
                raise SourceError('record_too_large')
            if not line.endswith(b'\n'):
                if offset + len(line) < info.st_size:
                    if not rows:
                        raise SourceError('byte_limit')
                    more = True
                else:
                    diagnostics.append('partial_line')
                break
            end = offset + len(line)
            try:
                obj = json.loads(contracts.canonicalize_text(line.decode('utf-8')))
                if not isinstance(obj, dict):
                    raise ValueError()
                row = self.project(obj, {'generation': gen, 'offset': offset})
            except (ValueError, KeyError, TypeError, RecursionError, contracts.RailsRefusal):
                row = self._notice('source.error', 'malformed_record', gen, offset, end)
            size = len(contracts.canonicalize(row))
            if size > MAX_RECORD:
                raise SourceError('record_too_large')
            if used + size > byte_limit:
                if not rows:
                    raise SourceError('byte_limit')
                more = True
                break
            rows.append(row)
            offset, used = end, used + size
        return result(rows, f'j1:{stamp}:{epoch}:{offset}',
                      more or (len(rows) == limit and offset < info.st_size),
                      str(info.st_size), diagnostics)
