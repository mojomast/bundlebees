"""CSV export for rails feedback and interventions (Rails R11).

Render an already authorized page with ``feedback_csv`` or ``intervention_csv``.
Only allowlisted scalar columns are rendered. The trusted local file helpers
export a complete (optionally target-filtered) history or refuse it as too
large; they never write a partial page and never serve as HTTP selectors.
"""

import csv
import io
import re

from rizzstack_rails import contracts, feedback, privacy

MAX_ROWS = 500
MAX_BYTES = 128 * 1024

FEEDBACK_FIELDS = (
    "event_id",
    "recorded_at",
    "feedback_id",
    "target_event_id",
    "revision",
    "operation",
    "rating",
    "category",
    "note",
    "cleared",
    "supersedes_event_id",
)

INTERVENTION_FIELDS = (
    "event_id",
    "recorded_at",
    "request_id",
    "kind",
    "operation",
    "decision",
    "target_event_id",
    "authority_ref",
    "queue_action_id",
    "reason",
)

# R12 exports these columns from its already filtered R10 query projection.
_QUERY_FIELDS = ("event_id", "kind", "lane_id", "outcome")
FORMULA_PREFIXES = ("=", "+", "-", "@")
_LABEL = re.compile(r"[A-Za-z0-9@=+._:\-]{1,192}")


def escape_cell(value):
    """Return a CSV-safe cell: formula-leading values are apostrophe-prefixed."""
    if value is None:
        return ""
    if isinstance(value, str):
        if len(value) > contracts.FEEDBACK_NOTE_MAX:
            raise feedback.FeedbackSizeRefusal("export cell exceeds size limit")
        # Reject control/format characters (including bidi and terminal escapes)
        # rather than hiding a formula prefix behind characters a sheet may trim.
        text = privacy.scrub_text(value) if value.isprintable() or not value else privacy.REDACTED
    elif type(value) is bool or type(value) is int and -contracts.INT_LIMIT <= value <= contracts.INT_LIMIT:
        text = str(value)
    else:
        raise feedback.FeedbackRefusal("export cells must be bounded scalar values")
    if text.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _check_limits(limit, max_bytes):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise feedback.FeedbackRefusal("export limit must be between 1 and 500")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise feedback.FeedbackRefusal("export max_bytes must be between 1 and 131072")


def _write_csv(rows, fields, defaults, limit, max_bytes):
    _check_limits(limit, max_bytes)
    fields = defaults if fields is None else fields
    allowed = set(defaults + _QUERY_FIELDS)
    if (not isinstance(fields, (tuple, list)) or not 1 <= len(fields) <= len(allowed)
            or any(not isinstance(f, str) or f not in allowed for f in fields)
            or len(set(fields)) != len(fields)):
        raise feedback.FeedbackRefusal("unknown or duplicate export columns")

    line = io.StringIO(newline="")
    writer = csv.writer(line, lineterminator="\n")
    parts, size = [], 0

    def write(cells):
        nonlocal size
        line.seek(0)
        line.truncate(0)
        writer.writerow(cells)
        text = line.getvalue()
        size += len(text.encode("utf-8"))
        if size > max_bytes:
            raise feedback.FeedbackSizeRefusal("CSV exceeds byte limit")
        parts.append(text)

    write(fields)
    for index, row in enumerate(rows):
        if index >= limit:
            raise feedback.FeedbackSizeRefusal("CSV exceeds row limit")
        if not isinstance(row, dict):
            raise feedback.FeedbackRefusal("export requires projected rows")
        cells = []
        for field in fields:
            value = row.get(field)
            if isinstance(value, str):
                if len(value) > contracts.FEEDBACK_NOTE_MAX:
                    raise feedback.FeedbackSizeRefusal("export cell exceeds size limit")
                if field in ("note", "reason"):
                    value = feedback.preview_note(value)
                elif value and (not _LABEL.fullmatch(value) or feedback._ADDRESS.search(value)):
                    value = privacy.REDACTED
            cells.append(escape_cell(value))
        write(cells)
    return "".join(parts)


def feedback_csv(rows, fields=None, *, limit=MAX_ROWS, max_bytes=MAX_BYTES):
    """Serialize a bounded page of projected rows, including R12 query rows."""
    return _write_csv(rows, fields, FEEDBACK_FIELDS, limit, max_bytes)


def intervention_csv(rows, fields=None, *, limit=MAX_ROWS, max_bytes=MAX_BYTES):
    """Serialize a bounded page of intervention rows."""
    return _write_csv(rows, fields, INTERVENTION_FIELDS, limit, max_bytes)


def export_feedback(ledger, path=None, target_event_id=None, fields=None, *,
                    limit=MAX_ROWS, max_bytes=MAX_BYTES):
    """Export all matching local overlay revisions or refuse (no truncation)."""
    _check_limits(limit, max_bytes)
    rows = feedback.history(ledger, target_event_id=target_event_id, limit=limit + 1)
    text = feedback_csv(rows, fields, limit=limit, max_bytes=max_bytes)
    if path is not None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text


def export_interventions(ledger, path=None, fields=None, *, target_event_id=None,
                         limit=MAX_ROWS, max_bytes=MAX_BYTES):
    """Export all matching local intervention rows or refuse (no truncation)."""
    _check_limits(limit, max_bytes)
    rows = feedback.intervention_events(ledger, target_event_id=target_event_id, limit=limit + 1)
    text = intervention_csv(rows, fields, limit=limit, max_bytes=max_bytes)
    if path is not None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text
