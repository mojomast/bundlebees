# Lease-time memory recall

Before the coordinator signs a lease it can ask the `rizzstack.memory/1` vault
for a small memory context and attach the result to the lease record. Memory is
advisory: it never changes the brief, the assigned files, the lease key, or the
MAC input that existed before this feature.

The feature is off by default. With `MEMORY_ENABLED` unset (or set to anything
other than exactly `1`) recall returns `None` and touches nothing: no
subprocess, no file, no miss log. A lease recorded with the flag off has no
`memory_context` key and is byte-identical to the pre-recall record shape.

## Flag and query command

| Name | Meaning |
| --- | --- |
| `MEMORY_ENABLED` | Exactly `1` enables recall. Every other value keeps it dark. |
| `BUNDLEBEES_MEMORY_QUERY` | Program, and optional arguments, that answers one query. |

`BUNDLEBEES_MEMORY_QUERY` is split like a shell word list and is **not** run
through a shell. The query text is appended as one argument, and the request
JSON is written to stdin. The child gets the minimal exact-name environment
from `security.child_env()`, never the ambient coordinator environment.

The query text is the brief's `objective`, falling back to `title`, `summary`,
then `description`. It is also carried in the request body so a program that
only reads stdin needs no argument handling.

Request on stdin:

```json
{
  "contract": "rizzstack.memory/1",
  "query": "<brief objective>",
  "caps": {
    "max_items": 8,
    "max_excerpt_chars": 480,
    "max_total_chars": 4000
  }
}
```

The program must exit zero within the bounded timeout (5 seconds) and write one
JSON query result to stdout. stderr is never read into a record or a log.

Response on stdout:

```json
{
  "contract": "rizzstack.memory/1",
  "vault_rev": "<vault revision>",
  "items": [
    {
      "id": "mem-1",
      "type": "note",
      "sha": "<content digest>",
      "path": "memory/note.md",
      "title": "Short title",
      "excerpt": "What the memory actually says."
    }
  ]
}
```

A different contract, a non-object payload, a non-list `items`, or a non-string
`vault_rev` is a miss. No memory is inferred from a malformed result.

## Caps

The client enforces the caps itself; it never trusts the query program to have
done so. `memory_context` always carries the cap block shown above.

* **Items**: at most 8. Items after the eighth are dropped.
* **Excerpts**: each excerpt is cut to at most 480 characters. This is the one
  truncation the contract applies to text.
* **Total**: the sum of the six retained field strings (`id`, `type`, `sha`,
  `path`, `title`, `excerpt`) across included items is at most 4000
  characters. Items that would cross the budget are dropped whole; an excerpt
  is never cut further just to make an item fit.
* **Fields**: an item missing any of `id`, `type`, `sha`, `path`, `title`, or
  `excerpt`, or carrying a non-string value for one, is dropped. Extra fields
  are discarded. Each surviving item has exactly those six keys.

An honest empty result stays an empty `items` list and is not a miss.

## Miss log

Every miss appends exactly one scrubbed JSON line to
`<state_dir>/memory/misses.jsonl`. The directory is private (`0700`), the file
is opened append-only with `O_NOFOLLOW` and created `0600`, and a logging
failure is swallowed so it can never stop a lease.

```json
{"at":"2026-01-02T03:04:05Z","code":"memory_query_failed","contract":"rizzstack.memory/1","schema":"rizzstack.memory-recall-miss/1"}
```

Miss codes:

* `memory_query_unconfigured` — the flag is on but no command is configured.
* `memory_query_failed` — the program could not start or exited nonzero.
* `memory_query_timeout` — the program exceeded the bounded timeout.
* `memory_query_invalid_json` — stdout was not a JSON object (or could not be
  decoded).
* `memory_query_wrong_contract` — the contract name was not
  `rizzstack.memory/1`.
* `memory_query_invalid_result` — `items` was not a list or `vault_rev` was not
  a string.
* `memory_query_invalid_output` — stdout could not be decoded as text.

The log deliberately contains no query text, brief, excerpt, item body,
configured command, stderr, path, or host. It is append-only: earlier lines are
never rewritten.

## Wiring

`record_lease()` calls recall after the brief is frozen and before the record is
MACed. A returned mapping is stored as `record["memory_context"]`; `None` adds
no key. Because the context is outside `brief`, `brief_sha256` and lease
verification are unchanged. Recall failures are caught, so dispatch always
proceeds with a normal lease and no invented memory.

## Rejected alternative

**Assemble memory on the worker.** Rejected: a worker's own claim is never
authority, the contract says bees do not assemble their own memory, and the
caps would be unenforced. Lease-time recall keeps the coordinator as the only
writer of the lease and the only place the caps are applied.
