/**
 * bundlebees Desktop pane.
 *
 * Loaded by the Hermes Desktop runtime loader, which allows only
 * "@hermes/plugin-sdk" and "react" imports and requires a default
 * HermesPlugin export ({ id, name, register(ctx) }). The pane is contributed
 * with ctx.register({ id, area: "panes", title, data, render }). Dark mode. Every request is namespace-relative and goes through
 * ctx.rest; the pane never builds an absolute URL, never calls a raw fetch
 * helper, and never asks for or renders a launcher token, bind address, or SSH host.
 */

import * as React from "react";

const PLUGIN_ID = "bundlebees";
const TITLE = "Bundlebees";
const DEPLOY_COMMAND = "hermes bundlebees deploy";

const TABS = Object.freeze([
  { id: "health", label: "Health" },
  { id: "containers", label: "Containers" },
  { id: "models", label: "Models" },
  { id: "concurrency", label: "Concurrency" },
  { id: "projects", label: "Projects" },
  { id: "attempts", label: "Live attempts" },
  { id: "verify-results", label: "Verify results" },
  { id: "host-prompt", label: "Remote host" },
]);

const COLORS = Object.freeze({
  background: "#0b0f14",
  surface: "#111821",
  surfaceAlt: "#17212c",
  border: "#243242",
  text: "#e6edf5",
  muted: "#93a4b8",
  accent: "#54d3a1",
  warning: "#f0b86e",
  error: "#f27f8b",
});

const STYLE_ID = "bundlebees-dark-pane";

const CSS = `
.bb-pane {
  color-scheme: dark;
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
  min-height: 520px;
  background: ${COLORS.background};
  color: ${COLORS.text};
  border: 1px solid ${COLORS.border};
  border-radius: 10px;
  overflow: hidden;
  font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
}
.bb-sidebar {
  background: ${COLORS.surface};
  border-right: 1px solid ${COLORS.border};
  padding: 14px 10px;
}
.bb-title {
  margin: 2px 8px 14px;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: 0.02em;
}
.bb-tab {
  display: block;
  width: 100%;
  padding: 8px 10px;
  margin: 2px 0;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: ${COLORS.muted};
  text-align: left;
  cursor: pointer;
}
.bb-tab:hover { background: ${COLORS.surfaceAlt}; color: ${COLORS.text}; }
.bb-tab[aria-selected="true"] {
  background: ${COLORS.surfaceAlt};
  color: ${COLORS.accent};
  font-weight: 600;
}
.bb-main { padding: 18px 20px; min-width: 0; }
.bb-heading { margin: 0 0 14px; font-size: 16px; font-weight: 650; }
.bb-card {
  background: ${COLORS.surface};
  border: 1px solid ${COLORS.border};
  border-radius: 9px;
  padding: 14px 16px;
  margin-bottom: 12px;
}
.bb-row { display: flex; gap: 10px; align-items: baseline; margin: 5px 0; }
.bb-key { color: ${COLORS.muted}; min-width: 128px; }
.bb-value { color: ${COLORS.text}; overflow-wrap: anywhere; }
.bb-error { color: ${COLORS.error}; }
.bb-ok { color: ${COLORS.accent}; }
.bb-warn { color: ${COLORS.warning}; }
.bb-list { margin: 0; padding-left: 18px; }
.bb-list li { margin: 4px 0; overflow-wrap: anywhere; }
.bb-input, .bb-select, .bb-note {
  background: ${COLORS.background};
  color: ${COLORS.text};
  border: 1px solid ${COLORS.border};
  border-radius: 7px;
  padding: 7px 9px;
  font: inherit;
}
.bb-note { width: 100%; min-height: 72px; resize: vertical; }
.bb-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.bb-button {
  background: ${COLORS.surfaceAlt};
  color: ${COLORS.text};
  border: 1px solid ${COLORS.border};
  border-radius: 7px;
  padding: 7px 11px;
  cursor: pointer;
}
.bb-button:hover { border-color: ${COLORS.accent}; }
.bb-button[disabled] { opacity: 0.5; cursor: default; }
.bb-code {
  background: ${COLORS.background};
  border: 1px solid ${COLORS.border};
  border-radius: 7px;
  padding: 9px 11px;
  color: ${COLORS.accent};
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-wrap: anywhere;
}
`;

function injectStyles() {
  if (typeof document === "undefined") return;
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

function relativePath(path) {
  if (typeof path !== "string") throw new TypeError("path must be a string");
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new TypeError("bundlebees pane paths must be namespace-relative");
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(path)) {
    throw new TypeError("absolute URLs are not allowed in the bundlebees pane");
  }
  return path;
}

function attemptPath(attemptId, suffix = "") {
  const id = encodeURIComponent(String(attemptId || "").trim());
  if (!id) throw new TypeError("an attempt id is required");
  return relativePath(`/attempts/${id}${suffix}`);
}

function createApi(ctx) {
  const rest = ctx && typeof ctx.rest === "function" ? ctx.rest.bind(ctx) : null;
  function requireRest() {
    if (!rest) {
      const error = new Error("the bundlebees dashboard backend is not available");
      error.code = "not_configured";
      throw error;
    }
    return rest;
  }
  return {
    health: () => requireRest()("/health"),
    containers: () => requireRest()("/containers"),
    models: () => requireRest()("/models"),
    concurrency: () => requireRest()("/concurrency"),
    setConcurrency: (maxConcurrent) =>
      requireRest()("/concurrency", {
        method: "PUT",
        body: { max_concurrent: maxConcurrent },
      }),
    projects: () => requireRest()("/projects"),
    attempts: () => requireRest()("/attempts"),
    attempt: (id) => requireRest()(attemptPath(id)),
    events: (id) => requireRest()(attemptPath(id, "/events")),
    cancel: (id) =>
      requireRest()(attemptPath(id, "/cancel"), {
        method: "POST",
        body: { confirm: true },
      }),
    review: (id, accepted, note) =>
      requireRest()(attemptPath(id, "/review"), {
        method: "POST",
        body: note
          ? { confirm: true, accepted, note }
          : { confirm: true, accepted },
      }),
    verifyResults: () => requireRest()("/verify-results"),
    hostPromptOptions: () => requireRest()("/host-prompt"),
    hostPrompt: (fields) =>
      requireRest()("/host-prompt", { method: "POST", body: fields }),
  };
}

function describeError(error) {
  if (!error) return { error: "unknown error", code: "unknown", fix: "" };
  return {
    error: String(error.error || error.message || error),
    code: String(error.code || "error"),
    fix: String(error.fix || ""),
  };
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function makeElement(ctx) {
  if (!React || typeof React.createElement !== "function") return null;
  const h = React.createElement;

  function Row({ label, children }) {
    return h(
      "div",
      { className: "bb-row" },
      h("span", { className: "bb-key" }, label),
      h("span", { className: "bb-value" }, children)
    );
  }

  function ErrorCard({ payload }) {
    if (!payload || payload.ok !== false) return null;
    const info = describeError(payload);
    return h(
      "div",
      { className: "bb-card bb-error" },
      h("div", { className: "bb-row" }, h("strong", null, info.error)),
      h(Row, { label: "code" }, info.code),
      info.fix ? h(Row, { label: "fix" }, info.fix) : null
    );
  }

  function ListCard({ title, items, render }) {
    return h(
      "div",
      { className: "bb-card" },
      h("div", { className: "bb-heading" }, title),
      items && items.length
        ? h("ul", { className: "bb-list" }, items.map(render))
        : h("span", { className: "bb-muted" }, "Nothing reported.")
    );
  }

  function HealthView({ data }) {
    const health = (data && data.health) || {};
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(
        "div",
        { className: "bb-card" },
        h("div", { className: "bb-heading" }, "Launcher health"),
        h(Row, { label: "state" }, valueText(health.ok)),
        h(Row, { label: "version" }, valueText(health.version)),
        h(Row, { label: "model" }, valueText(health.model)),
        h(Row, { label: "active" }, valueText(health.active)),
        h(Row, { label: "queued" }, valueText(health.queued)),
        h(Row, { label: "max concurrent" }, valueText(health.max_concurrent))
      )
    );
  }

  function ContainersView({ data }) {
    const items = (data && data.containers) || [];
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(ListCard, {
        title: "Worker containers",
        items,
        render: (item, index) =>
          h(
            "li",
            { key: index },
            h("div", null, h("strong", null, valueText(item.runtime))),
            h(Row, { label: "digest" }, valueText(item.digest)),
            h(Row, { label: "memory_mb" }, valueText(item.memory_mb)),
            h(Row, { label: "toolchain" }, valueText(item.toolchain))
          ),
      })
    );
  }

  function ModelsView({ data }) {
    const items = (data && data.models) || [];
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(ListCard, {
        title: "Allowed model ids",
        items,
        render: (item, index) => h("li", { key: index }, valueText(item)),
      })
    );
  }

  function ConcurrencyView({ data, onSave, busy }) {
    const [value, setValue] = React.useState(
      data && data.max_concurrent ? String(data.max_concurrent) : ""
    );
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(
        "div",
        { className: "bb-card" },
        h("div", { className: "bb-heading" }, "Runtime concurrency"),
        h(Row, { label: "current" }, valueText(data && data.max_concurrent)),
        h(Row, { label: "persisted" }, valueText(data && data.persisted)),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "max_concurrent"),
          h("input", {
            className: "bb-input",
            type: "number",
            min: 1,
            max: 10,
            value,
            onChange: (event) => setValue(event.target.value),
          })
        ),
        h("p", { className: "bb-warn" }, (data && data.note) || ""),
        h(
          "div",
          { className: "bb-actions" },
          h(
            "button",
            {
              className: "bb-button",
              disabled: busy,
              onClick: () => onSave(Number(value)),
            },
            "Apply"
          )
        )
      )
    );
  }

  function ProjectsView({ data }) {
    const items = (data && data.projects) || [];
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(ListCard, {
        title: "Authorized projects",
        items,
        render: (item, index) =>
          h(
            "li",
            { key: index },
            h("div", null, h("strong", null, valueText(item.key))),
            h(Row, { label: "public repo" }, valueText(item.repo)),
            h(Row, { label: "sync branch" }, valueText(item.sync_branch))
          ),
      })
    );
  }

  function AttemptsView({ data, detail, events, onSelect, onCancel }) {
    const items = (data && data.attempts) || [];
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(ListCard, {
        title: "Live attempts",
        items,
        render: (item, index) =>
          h(
            "li",
            { key: index },
            h(
              "button",
              {
                className: "bb-tab",
                onClick: () => onSelect(item.attempt_id),
              },
              `${valueText(item.attempt_id)} · ${valueText(item.state)}`
            ),
            h(Row, { label: "project" }, valueText(item.project)),
            h(Row, { label: "branch" }, valueText(item.branch))
          ),
      }),
      detail
        ? h(
            "div",
            { className: "bb-card" },
            h("div", { className: "bb-heading" }, "Attempt detail"),
            h(Row, { label: "attempt" }, valueText(detail.attempt && detail.attempt.attempt_id)),
            h(Row, { label: "state" }, valueText(detail.attempt && detail.attempt.state)),
            h(Row, { label: "detail" }, valueText(detail.attempt && detail.attempt.detail)),
            detail.result
              ? h(Row, { label: "changed files" }, valueText(detail.result.observed && detail.result.observed.changed_files))
              : null,
            h(
              "div",
              { className: "bb-actions" },
              h(
                "button",
                {
                  className: "bb-button",
                  onClick: () => onCancel(detail.attempt && detail.attempt.attempt_id),
                },
                "Cancel attempt"
              )
            ),
            events && events.length
              ? h(
                  "ul",
                  { className: "bb-list" },
                  events.map((event, index) =>
                    h("li", { key: index }, valueText(event.text))
                  )
                )
              : null
          )
        : null
    );
  }

  function VerifyView({ data }) {
    const items = (data && data.results) || [];
    return h(
      "div",
      null,
      h(ErrorCard, { payload: data }),
      h(ListCard, {
        title: "Verify results",
        items,
        render: (item, index) =>
          h(
            "li",
            { key: index },
            h("div", null, h("strong", null, valueText(item.attempt_id))),
            h(Row, { label: "ok" }, valueText(item.ok)),
            h(Row, { label: "sha" }, valueText(item.sha)),
            h(Row, { label: "branch" }, valueText(item.branch)),
            h(Row, { label: "files" }, valueText(item.files)),
            h(Row, { label: "tests" }, valueText((item.tests || []).map((test) => test.name)))
          ),
      })
    );
  }

  function HostPromptView({ options, result, onSubmit, busy }) {
    const [os, setOs] = React.useState("linux");
    const [reach, setReach] = React.useState("same-machine");
    const [provider, setProvider] = React.useState("opencode-go");
    const [repos, setRepos] = React.useState("");
    const [maxConcurrent, setMaxConcurrent] = React.useState("2");
    return h(
      "div",
      null,
      h(ErrorCard, { payload: options }),
      h(ErrorCard, { payload: result }),
      h(
        "div",
        { className: "bb-card" },
        h("div", { className: "bb-heading" }, "Remote host setup prompt"),
        h("p", { className: "bb-warn" },
          "Connectivity is preconfigured. Do not enter a launcher URL, token, or SSH host."
        ),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "os"),
          h(
            "select",
            {
              className: "bb-select",
              value: os,
              onChange: (event) => setOs(event.target.value),
            },
            ((options && options.os_options) || ["linux", "macos", "windows"]).map(
              (item) => h("option", { key: item, value: item }, item)
            )
          )
        ),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "reach method"),
          h(
            "select",
            {
              className: "bb-select",
              value: reach,
              onChange: (event) => setReach(event.target.value),
            },
            ((options && options.reach_options) || ["same-machine", "lan", "vpn", "ssh"]).map(
              (item) => h("option", { key: item, value: item }, item)
            )
          )
        ),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "provider"),
          h("input", {
            className: "bb-input",
            value: provider,
            onChange: (event) => setProvider(event.target.value),
          })
        ),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "repositories"),
          h("textarea", {
            className: "bb-note",
            placeholder: "owner/name or public repository URLs, one per line",
            value: repos,
            onChange: (event) => setRepos(event.target.value),
          })
        ),
        h("div", { className: "bb-row" },
          h("span", { className: "bb-key" }, "max concurrent"),
          h("input", {
            className: "bb-input",
            type: "number",
            min: 1,
            max: 10,
            value: maxConcurrent,
            onChange: (event) => setMaxConcurrent(event.target.value),
          })
        ),
        h(
          "div",
          { className: "bb-actions" },
          h(
            "button",
            {
              className: "bb-button",
              disabled: busy,
              onClick: () =>
                onSubmit({
                  os,
                  reach,
                  provider,
                  repos: repos
                    .split(/\r?\n/)
                    .map((item) => item.trim())
                    .filter(Boolean),
                  max_concurrent: Number(maxConcurrent),
                }),
            },
            "Generate prompt"
          )
        ),
        h("div", { className: "bb-heading" }, "Deploy"),
        h("div", { className: "bb-code" }, DEPLOY_COMMAND),
        result && result.prompt
          ? h("pre", { className: "bb-card" }, result.prompt)
          : null
      )
    );
  }

  function BundlebeesPane() {
    const api = React.useMemo(() => createApi(ctx), [ctx]);
    const [tab, setTab] = React.useState("health");
    const [state, setState] = React.useState({});
    const [detail, setDetail] = React.useState(null);
    const [events, setEvents] = React.useState([]);
    const [busy, setBusy] = React.useState(false);

    React.useEffect(() => {
      injectStyles();
    }, []);

    React.useEffect(() => {
      let alive = true;
      const load = async () => {
        try {
          let data;
          if (tab === "health") data = await api.health();
          else if (tab === "containers") data = await api.containers();
          else if (tab === "models") data = await api.models();
          else if (tab === "concurrency") data = await api.concurrency();
          else if (tab === "projects") data = await api.projects();
          else if (tab === "attempts") data = await api.attempts();
          else if (tab === "verify-results") data = await api.verifyResults();
          else data = await api.hostPromptOptions();
          if (alive) setState((current) => ({ ...current, [tab]: data }));
        } catch (error) {
          if (alive) {
            setState((current) => ({
              ...current,
              [tab]: { ok: false, ...describeError(error) },
            }));
          }
        }
      };
      load();
      const timer = setInterval(load, 10000);
      return () => {
        alive = false;
        clearInterval(timer);
      };
    }, [tab, api]);

    const saveConcurrency = async (value) => {
      setBusy(true);
      try {
        const data = await api.setConcurrency(value);
        setState((current) => ({ ...current, concurrency: data }));
      } catch (error) {
        setState((current) => ({
          ...current,
          concurrency: { ok: false, ...describeError(error) },
        }));
      } finally {
        setBusy(false);
      }
    };

    const selectAttempt = async (attemptId) => {
      setBusy(true);
      try {
        const [attemptData, eventData] = await Promise.all([
          api.attempt(attemptId),
          api.events(attemptId),
        ]);
        setDetail(attemptData);
        setEvents((eventData && eventData.events) || []);
      } catch (error) {
        setDetail({ ok: false, ...describeError(error) });
        setEvents([]);
      } finally {
        setBusy(false);
      }
    };

    const cancelAttempt = async (attemptId) => {
      if (!attemptId) return;
      setBusy(true);
      try {
        await api.cancel(attemptId);
        await selectAttempt(attemptId);
      } catch (error) {
        setDetail({ ok: false, ...describeError(error) });
      } finally {
        setBusy(false);
      }
    };

    const submitHostPrompt = async (fields) => {
      setBusy(true);
      try {
        const data = await api.hostPrompt(fields);
        setState((current) => ({ ...current, "host-prompt": data }));
      } catch (error) {
        setState((current) => ({
          ...current,
          "host-prompt": { ok: false, ...describeError(error) },
        }));
      } finally {
        setBusy(false);
      }
    };

    let content = null;
    if (tab === "health") content = h(HealthView, { data: state.health });
    else if (tab === "containers") content = h(ContainersView, { data: state.containers });
    else if (tab === "models") content = h(ModelsView, { data: state.models });
    else if (tab === "concurrency")
      content = h(ConcurrencyView, {
        data: state.concurrency,
        onSave: saveConcurrency,
        busy,
      });
    else if (tab === "projects") content = h(ProjectsView, { data: state.projects });
    else if (tab === "attempts")
      content = h(AttemptsView, {
        data: state.attempts,
        detail,
        events,
        onSelect: selectAttempt,
        onCancel: cancelAttempt,
      });
    else if (tab === "verify-results")
      content = h(VerifyView, { data: state["verify-results"] });
    else
      content = h(HostPromptView, {
        options: state["host-prompt"],
        result: state["host-prompt"],
        onSubmit: submitHostPrompt,
        busy,
      });

    return h(
      "section",
      { className: "bb-pane", "data-plugin": PLUGIN_ID, "data-theme": "dark" },
      h(
        "nav",
        { className: "bb-sidebar" },
        h("div", { className: "bb-title" }, TITLE),
        TABS.map((item) =>
          h(
            "button",
            {
              key: item.id,
              className: "bb-tab",
              "aria-selected": tab === item.id ? "true" : "false",
              onClick: () => setTab(item.id),
            },
            item.label
          )
        )
      ),
      h("main", { className: "bb-main" }, content)
    );
  }

  return BundlebeesPane;
}

export function createPane(ctx) {
  return makeElement(ctx);
}

/** Contribute the pane through the SDK's ctx.register. */
export function registerBundlebees(ctx) {
  if (!ctx || typeof ctx.register !== "function") return false;
  const Pane = makeElement(ctx);
  if (!Pane) return false;
  if (typeof ctx.onDispose === "function") {
    ctx.onDispose(() => {
      if (typeof document === "undefined") return;
      const style = document.getElementById(STYLE_ID);
      if (style) style.remove();
    });
  }
  injectStyles();
  ctx.register({
    id: "pane",
    area: "panes",
    title: TITLE,
    order: 44,
    data: { placement: "right", width: "560px" },
    render: () => React.createElement(Pane, {}),
  });
  return true;
}

export const manifest = Object.freeze({
  id: PLUGIN_ID,
  title: TITLE,
  darkMode: true,
  tabs: TABS,
  deployCommand: DEPLOY_COMMAND,
});

const plugin = {
  id: PLUGIN_ID,
  name: TITLE,
  description:
    "Launcher health, containers, models, concurrency, projects, attempts, verify results, and the host prompt.",
  register: registerBundlebees,
};

export default plugin;
