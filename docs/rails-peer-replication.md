# Read a worker's Rails ledger

`bundlebees_plugin.rails_remote_source.RemoteRailSource` pulls pages from an explicitly enrolled launcher. The origin is selected by operator configuration, never by a Rails query selector. Use the expected chain ID from enrollment; do not adopt a different chain when a worker restarts.

```python
from bundlebees_plugin.rails_remote_source import RemoteRailSource
from rizzstack_rails.ledger import RailsLedger

source = RemoteRailSource(config=launcher_settings, peer_id="worker-1")
ledger = RailsLedger(coordinator_db)
try:
    page = source.import_into(ledger, enrolled_chain_id, limit=100)
    # Schedule another bounded page if page['has_more']; do not advance
    # an application-owned cursor or count checkpoints as worker activity.
finally:
    ledger.close()
```

`launcher_settings['launcher_url']` identifies the origin authorized to receive the dedicated Hermes-scoped `BUNDLEBEES_RAILS_READ_TOKEN`. Configure that token to match the worker's `rails_read_token_file`; the launcher control token is not used. For a separately enrolled peer, pass an explicit `token_provider` bound to that peer. The client uses TLS or a literal loopback tunnel, the launcher's pinned-address transport, and the configured private-address policy. Each request is bounded and does not follow redirects.

`import_into()` resumes from SQLite's committed import receipt. `head` in a page describes the **advertised** tip; a partial page does not prove data up to that tip. Missing ranges, rewritten committed history or a reset behind the receipt require reconciliation. Workers supply observations: a worker export cannot record an owner decision or verifier verdict on their behalf. Read failures do not advance the ledger receipt.
