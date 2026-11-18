"""Markdown from structured public marks only; never interpolate summaries or notes."""
from .projection import public_id


def render_markdown(items):
    lines = ['Rails observations (untrusted source data):']
    for item in items:
        # Inline code spans around validated tokens prevent links/formatting.
        values = [item['event_id'],item['kind'],item.get('command_family'),item.get('outcome')]
        values.extend(item.get('evidence_refs', ()))
        if item.get('current') is False:
            values.append('not-current')
        elif 'current' in item and item['current'] is None:
            values.append('current-unknown')
        if item.get('ghost'):
            values.append('superseded')
        if item.get('duration_ms') is not None:
            values += [str(item['duration_ms'])+'ms',item['timing_quality'],item.get('latency')]
        u = item.get('usage')
        if u and u['counted']:
            values += [f"{k}:{v if v is not None else 'unknown'}" for k,v in u.items() if k.endswith('_tokens')]
        lines.append('- ' + ' '.join('`'+v+'`' for v in values if public_id(v)))
    return '\n'.join(lines)
