"""CSV export for rails feedback and interventions (Rails R11).

Text cells pass through :func:`escape_cell` before serialization so a
spreadsheet never evaluates owner-authored text as a formula. Cells whose
first character is ``=``, ``+``, ``-`` or ``@`` are prefixed with an
apostrophe, which spreadsheet programs treat as literal text. Non-text values
(including negative numbers) keep their native CSV representation.

Exports are deterministic and write with ``newline=""`` so the generated
bytes do not depend on the host line-ending convention.
"""

import csv
import io

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

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_cell(value):
    """Return a CSV-safe cell: formula-leading values are apostrophe-prefixed."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _write_csv(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        writer.writerow([escape_cell(row.get(field)) for field in fields])
    return buffer.getvalue()


def feedback_csv(rows, fields=None):
    """Serialize feedback rows to CSV, escaping every text cell."""
    return _write_csv(rows, tuple(fields) if fields is not None else FEEDBACK_FIELDS)


def intervention_csv(rows, fields=None):
    """Serialize intervention rows to CSV, escaping every text cell."""
    return _write_csv(rows, tuple(fields) if fields is not None else INTERVENTION_FIELDS)


def export_feedback(ledger, path=None, target_event_id=None, fields=None):
    """Export feedback history for a ledger, optionally writing ``path``."""
    from rizzstack_rails import feedback as rails_feedback

    rows = rails_feedback.history(ledger, target_event_id=target_event_id)
    text = feedback_csv(rows, fields)
    if path is not None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text


def export_interventions(ledger, path=None, fields=None):
    """Export intervention events for a ledger, optionally writing ``path``."""
    from rizzstack_rails import feedback as rails_feedback

    rows = rails_feedback.intervention_events(ledger)
    text = intervention_csv(rows, fields)
    if path is not None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text
