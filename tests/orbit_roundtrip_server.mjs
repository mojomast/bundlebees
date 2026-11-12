// Test fixture: the REAL Orbit /api/local-workers route (imported from an orbitdesktop checkout given by
// ORBIT_SRC) in front of a minimal stand-in for the PC launcher's /v1 API. The stand-in finishes every
// attempt immediately and returns a result manifest whose patch is read from ROUNDTRIP_PATCH_FILE.
// Prints one JSON line {"url": ...} when ready. Used by tests/test_orbit_roundtrip.py only.
import http from 'node:http';
import path from 'node:path';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const src = process.env.ORBIT_SRC;
const runtimeRoot = process.env.ROUNDTRIP_RUNTIME;
const launcherToken = 'roundtrip-launcher-token';
const { createLocalWorkersRoute } = await import(pathToFileURL(path.join(src, 'server', 'local-workers-route.mjs')).href);

const attempts = {};
const launcher = http.createServer(async (req, res) => {
  let body = ''; for await (const c of req) body += c;
  const send = (code, o) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); };
  if (req.headers.authorization !== `Bearer ${launcherToken}`) return send(401, { error: 'unauthorized' });
  const u = new URL(req.url, 'http://x');
  if (req.method === 'POST' && u.pathname === '/v1/attempts') {
    const b = JSON.parse(body); attempts[b.attempt_id] = { brief: b.brief, model: b.model };
    return send(201, { attempt_id: b.attempt_id, state: 'running', run_id: 'run-rt', detail: 'running' });
  }
  if (u.pathname === '/v1/config') return send(404, {});
  const m = u.pathname.match(/^\/v1\/attempts\/([^/]+)(\/events|\/result|\/cancel)?$/);
  const a = m && attempts[m[1]]; if (!a) return send(404, { error: 'unknown' });
  if (m[2] === '/events') return send(200, { events: [], cursor: 0 });
  if (m[2] === '/result') {
    const diff = readFileSync(process.env.ROUNDTRIP_PATCH_FILE, 'utf8');
    return send(200, {
      schema: 'orbit.local-worker.result/v1', attempt_id: m[1],
      source: { project: a.brief.project || null, repo_rev: a.brief.source_rev, model: a.model, launcher: 'roundtrip-fake' },
      observed: { run_id: 'run-rt', local_base_rev: a.brief.source_rev, status: 'completed', stop_reason: 'end_turn', tool_calls: 3,
        usage: JSON.stringify({ inputTokens: 1000, outputTokens: 200, totalTokens: 1200 }), files: [{ path: 'src/a.py', sha256: 'f'.repeat(64), change: 'changed', in_assigned_scope: true }] },
      patch: { diff, files: [{ path: 'src/a.py' }] },
      worker_claims: { summary: 'scripted worker' }, note: 'roundtrip fixture',
    });
  }
  return send(200, { attempt_id: m[1], state: 'succeeded', detail: 'turn ended: end_turn' });
});
await new Promise((r) => launcher.listen(Number(process.env.ROUNDTRIP_LAUNCHER_PORT || 0), '127.0.0.1', r));
mkdirSync(runtimeRoot, { recursive: true });
const tokenFile = path.join(runtimeRoot, 'launcher.token'); writeFileSync(tokenFile, launcherToken);
const route = createLocalWorkersRoute({ runtimeRoot, port: 1, env: {
  ORBIT_LOCAL_WORKERS_URL: `http://127.0.0.1:${launcher.address().port}`, ORBIT_LOCAL_WORKERS_TOKEN_FILE: tokenFile,
  ORBIT_LOCAL_WORKERS_POLL_MS: '200', ORBIT_LOCAL_WORKERS_MAX: '4' } });
const orbit = http.createServer((req, res) => route.handle(req, res));
await new Promise((r) => orbit.listen(Number(process.env.ROUNDTRIP_ORBIT_PORT || 0), '127.0.0.1', r));
process.stdout.write(JSON.stringify({ url: `http://127.0.0.1:${orbit.address().port}`, launcher_port: launcher.address().port }) + '\n');
