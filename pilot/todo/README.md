# Pilot todo application

A disposable, dependency-free todo app used to exercise the rizzstack factory
end to end: persistent storage, an HTTP API, a browser UI, validation, search
and application-level acceptance.

Status: **fixture/pilot source, not a deployed service**. It is built through
the production job/queue/driver path; the real-bee run remains blocked until
the worker deployment, policy enrollment and real-TTY authorization in
`OPERATOR-ACTION-BUNDLE.md` are complete.

## Run

```bash
python -m pilot.todo.server --db ./todos.json --port 8080
# open http://127.0.0.1:8080/
```

The storage file is created on first write. The server and UI use only the
Python standard library and no external assets.

## Scope

| Feature | Files | Acceptance |
|---|---|---|
| Persistent store | `pilot/todo/store.py` | atomic JSON, CRUD, filtering, validation |
| HTTP API | `pilot/todo/server.py` | create/read/update/complete, search/filter, JSON errors |
| Browser UI | `pilot/todo/static/index.html` | create, edit, complete, search/filter, accessible labels |
| Application acceptance | `pilot/todo/acceptance.py` | real server + HTTP + API contract checks |

The executable plan is `pilot/todo/project.json`; the project planner turns it
into a dependent task list and the factory driver advances it.
