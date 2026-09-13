// Component/network fixture, not authenticated Von or physical-phone acceptance.
// Uses the production outage UI, health evaluator, offline shell and service worker.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface/static');
const evidence = process.argv[2];
assert(evidence, 'Provide evidence directory');
fs.mkdirSync(evidence, { recursive: true });
let available = true;
let effects = 0;
let status = {
  schema_version: 'von_maintenance.v1', release_id: 'component-fixture', agent: 'Codex DGX fixture',
  state: 'planned', reason: 'Controlled maintenance fixture', planned_start: new Date().toISOString(),
  estimated_ready_at: new Date(Date.now() + 180000).toISOString(),
  updated_at: new Date().toISOString(), expires_at: new Date(Date.now() + 1800000).toISOString()
};
const app = `<!doctype html><meta name="von-app-shell" content="1"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/outage/outage.css"><main><div><textarea id="promptInput" aria-label="Draft"></textarea></div></main><script type="module">
import { installOutageView } from '/static/outage/app.js';
import { preserveOutageDraft } from '/static/outage/draft.js';
import { evaluateServerHealthState } from '/static/js/utils/serverHealthState.js';
installOutageView();
preserveOutageDraft(() => 'fixture-actor@fixture-org');
window.health = params => {
 const result = evaluateServerHealthState(params);
 document.dispatchEvent(new CustomEvent('von:healthPollDiagnostics', { detail: { state: result.state, ...result.diagnostics } }));
};
document.addEventListener('von:requestHealthCheck', async () => {
 const response = await fetch('/health');
 window.health({ hasSeenSuccessfulHealthPoll: true, failureCount: response.ok ? 0 : 4, firstFailureAtMs: Date.now()-30000 });
});
</script>`;
const server = http.createServer((req, res) => {
  if (req.method !== 'GET') effects++;
  const pathname = new URL(req.url, 'http://localhost').pathname;
  // This route models an independently served public file, with no write endpoint.
  if (pathname === '/von-status/maintenance.json') {
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(status)); return;
  }
  if (!available) { res.writeHead(503); res.end('Backend unavailable'); return; }
  if (pathname === '/health') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ status: 'healthy', start_time: 'fixture' })); return;
  }
  if (pathname === '/von/') { res.setHeader('Content-Type', 'text/html'); res.end(app); return; }
  let file;
  if (pathname === '/von/outage-worker.js') file = path.join(root, 'outage/service-worker.js');
  else if (pathname.startsWith('/static/')) file = path.resolve(root, pathname.slice(8));
  if (!file?.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  const mime = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[path.extname(file)];
  res.setHeader('Content-Type', mime || 'text/plain');
  res.setHeader('X-Fixture-Private-Header', 'must-not-be-cached');
  res.end(fs.readFileSync(file));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await page.goto(`${base}/von/`);
    await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
    });
    const draft = page.locator('#promptInput');
    await draft.fill('Unsent local acceptance draft');
    await page.evaluate(() => window.health({ hasSeenSuccessfulHealthPoll: true, failureCount: 1, firstFailureAtMs: Date.now() }));
    await expect(page.locator('dialog')).not.toBeVisible();
    await page.evaluate(() => window.health({ hasSeenSuccessfulHealthPoll: true, failureCount: 8, firstFailureAtMs: Date.now()-120000, isThinkingActive: true }));
    await expect(page.locator('dialog')).not.toBeVisible();
    available = false;
    await page.evaluate(() => window.health({ hasSeenSuccessfulHealthPoll: true, failureCount: 4, firstFailureAtMs: Date.now()-30000 }));
    await expect(page.locator('dialog')).toBeVisible();
    await expect(page.locator('[data-maintenance]')).toContainText('Codex DGX fixture');
    for (const [label, width, height] of [['portrait',390,844], ['keyboard',390,350], ['landscape',844,390]]) {
      await page.setViewportSize({ width, height });
      await page.locator('[data-retry]').click();
      await expect(draft).toHaveValue('Unsent local acceptance draft');
      assert(await page.evaluate(() => document.querySelector('dialog').scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(evidence, `${label}.png`) });
    }
    available = true;
    status = { ...status, state: 'ready', estimated_ready_at: null };
    await page.locator('[data-retry]').click();
    await expect(page.locator('dialog')).not.toBeVisible();
    await expect(draft).toBeFocused();
    await expect(draft).toHaveValue('Unsent local acceptance draft');
    // Reload/reopen acceptance is separate from in-memory draft retention.
    available = false;
    await page.reload();
    await expect(page.locator('h1')).toHaveText('Cannot reach Von');
    await expect(page.locator('#checked')).toContainText('Last checked');
    await context.setOffline(true);
    await page.locator('#retry').click();
    await expect(page.locator('#connection')).toContainText('device reports that it is offline');
    const reopened = await context.newPage();
    await reopened.goto(`${base}/von/`);
    await expect(reopened.locator('h1')).toHaveText('Cannot reach Von');
    const cached = await page.evaluate(async () => {
      const entries = [];
      for (const name of await caches.keys()) for (const request of await (await caches.open(name)).keys()) {
        const response = await (await caches.open(name)).match(request);
        if (response.headers.has('X-Fixture-Private-Header')) throw new Error('Non-public response header cached');
        entries.push(new URL(request.url).pathname);
      }
      return entries.sort();
    });
    assert.deepEqual(cached, ['/static/outage/offline.html','/static/outage/offline.js','/static/outage/outage.css','/static/outage/status.js']);
    available = true;
    await context.setOffline(false);
    await expect(page.locator('#promptInput')).toBeVisible();
    await page.locator('#vonRecoveredDraft summary').click();
    await expect(page.getByLabel('Recovered unsent draft')).toHaveValue('Unsent local acceptance draft');
    await expect(page.locator('#promptInput')).toHaveValue('');
    assert.equal(effects, 0);
    const result = { surface: 'component/network fixture', viewports: 3, transientNoOverlay: true, thinkingNoOverlay: true,
      openDraftRetained: true, reloadDraftRecovery: true, focusRestored: true, offlineReloadAndReopen: true, reconnect: true, cachedPaths: cached, effects };
    fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
