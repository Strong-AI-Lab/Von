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
let accessGate = false;
let signInBase;
let base;
let status = {
  schema_version: 'von_maintenance.v1', release_id: 'component-fixture', agent: 'Codex DGX fixture',
  state: 'planned', reason: 'Controlled maintenance fixture', planned_start: new Date().toISOString(),
  estimated_ready_at: new Date(Date.now() + 180000).toISOString(),
  updated_at: new Date().toISOString(), expires_at: new Date(Date.now() + 1800000).toISOString()
};
const app = `<!doctype html><meta name="von-app-shell" content="1"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/outage/outage.css"><main><div id="conversation" data-session="fixture-conversation">Existing conversation</div><input id="attachment" type="file"><div><textarea id="promptInput" aria-label="Draft"></textarea></div></main><script type="module">
import { installOutageView } from '/static/outage/app.js';
import { preserveOutageDraft } from '/static/outage/draft.js';
import { fetchVonHealth, healthErrorKind } from '/static/outage/health.js';
import { evaluateServerHealthState } from '/static/js/utils/serverHealthState.js';
installOutageView();
preserveOutageDraft(() => 'fixture-actor@fixture-org');
window.health = params => {
 const result = evaluateServerHealthState(params);
 document.dispatchEvent(new CustomEvent('von:healthPollDiagnostics', { detail: { state: result.state, ...result.diagnostics } }));
};
window.checkHealth = async () => {
 let errorKind = null;
 try { await fetchVonHealth(); } catch (error) { errorKind = healthErrorKind(error); }
 window.health({ hasSeenSuccessfulHealthPoll: true, failureCount: errorKind ? 4 : 0,
   firstFailureAtMs: Date.now()-30000, latestErrorKind: errorKind });
 return errorKind;
};
document.addEventListener('von:requestHealthCheck', window.checkHealth);
document.addEventListener('von:connectionRestored', async () => {
 const conversation = document.getElementById('conversation');
 const response = await fetch('/von/history?session_id=' + conversation.dataset.session);
 if (response.ok) conversation.textContent = (await response.json()).text;
});
</script>`;
const server = http.createServer((req, res) => {
  if (req.method !== 'GET') effects++;
  const pathname = new URL(req.url, 'http://localhost').pathname;
  // This route models an independently served public file, with no write endpoint.
  if (pathname === '/von-status/maintenance.json') {
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(status)); return;
  }
  if (pathname === '/fixture-sign-in-complete') {
    res.writeHead(302, { 'Set-Cookie': 'fixture_access=valid; HttpOnly; SameSite=Lax; Path=/', Location: '/von/' }); res.end(); return;
  }
  if (accessGate && !req.headers.cookie?.includes('fixture_access=valid')) {
    res.writeHead(302, { Location: signInBase }); res.end(); return;
  }
  if (!available) { res.writeHead(503); res.end('Backend unavailable'); return; }
  if (pathname === '/health') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ status: 'healthy', start_time: 'fixture',
      runtime_authority: { startup_seed_materialisations: { ready: false, state: 'skipped' } }
    })); return;
  }
  if (pathname === '/von/history') {
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ text: 'Recovered fixture conversation' })); return;
  }
  if (pathname === '/von/') { res.setHeader('Content-Type', 'text/html'); res.end(app); return; }
  let file;
  if (pathname === '/von/service-worker.js') file = path.join(root, 'service-worker.js');
  else if (pathname === '/von/outage-worker.js') file = path.join(root, 'outage/service-worker.js');
  else if (pathname.startsWith('/static/')) file = path.resolve(root, pathname.slice(8));
  if (!file?.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
  const mime = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[path.extname(file)];
  res.setHeader('Content-Type', mime || 'text/plain');
  res.setHeader('X-Fixture-Private-Header', 'must-not-be-cached');
  res.end(fs.readFileSync(file));
});
// A separate origin without CORS headers reproduces Access-style redirect fetch
// failure. This is a disposable HTTP/cookie fixture, not Cloudflare or Google OAuth.
const signInServer = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end(`<h1>Fixture sign-in</h1><a href="${base}/fixture-sign-in-complete">Continue with fixture account</a>`);
});
(async () => {
  await new Promise(resolve => signInServer.listen(0, '127.0.0.1', resolve));
  signInBase = `http://127.0.0.1:${signInServer.address().port}`;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await page.goto(`${base}/von/`);
    await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
    });
    const workerLifecycle = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration('/von/');
      const cache = await caches.open('von-push-state-v1');
      await cache.put('/von/.push-binding', new Response('{}'));
      await new Promise((resolve, reject) => {
        const channel = new MessageChannel();
        const timer = setTimeout(() => reject(new Error('Combined worker disable did not reply')), 5000);
        channel.port1.onmessage = event => {
          clearTimeout(timer);
          event.data?.ok ? resolve() : reject(new Error('Disable failed'));
        };
        reg.active.postMessage({ type: 'von-push-disable' }, [channel.port2]);
      });
      return { script: reg.active.scriptURL, pushStateCleared: !(await caches.has('von-push-state-v1')) };
    });
    assert.equal(workerLifecycle.script, `${base}/von/service-worker.js`);
    assert.equal(workerLifecycle.pushStateCleared, true);
    const draft = page.locator('#promptInput');
    assert.equal(await page.evaluate(() => window.checkHealth()), null);
    await expect(page.locator('dialog')).not.toBeVisible();
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
      await expect(page.locator('[data-connection]')).toContainText('gateway returned an error');
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
    // Invalidate only this isolated browser's fixture cookie. No real profile,
    // credentials, production logout or model prompts are involved.
    accessGate = true;
    await context.clearCookies();
    await page.locator('#attachment').setInputFiles({ name: 'unsent.txt', mimeType: 'text/plain', buffer: Buffer.from('Unsent attachment') });
    const baselineFailure = await page.evaluate(async () => {
      try { await fetch('/health'); return 'unexpected success'; } catch (error) { return error.name; }
    });
    assert.equal(baselineFailure, 'TypeError');
    assert.equal(await page.evaluate(() => window.checkHealth()), 'redirect');
    await expect(page.locator('[data-connection]')).toContainText('may need to sign in again');
    await expect(page.locator('[data-connection]')).toContainText('does not confirm a Von outage');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(evidence, 'access-expired.png') });
    await page.setViewportSize({ width: 1280, height: 900 });
    assert(await page.evaluate(() => document.querySelector('dialog').scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(evidence, 'access-expired-desktop.png') });
    const originalUrl = page.url();
    const newTabPromise = context.waitForEvent('page');
    await page.getByRole('link', { name: 'Sign in again in a new tab' }).click();
    const signInTab = await newTabPromise;
    await expect(signInTab.getByRole('heading')).toHaveText('Fixture sign-in');
    assert.equal(await signInTab.evaluate(() => window.opener), null);
    await signInTab.getByRole('link', { name: 'Continue with fixture account' }).click();
    await expect(signInTab.locator('#promptInput')).toBeVisible();
    await page.bringToFront();
    // Explicit retry also works where the browser does not emit a focus event.
    if (await page.locator('dialog').isVisible()) await page.locator('[data-retry]').click();
    await expect(page.locator('dialog')).not.toBeVisible();
    await expect(page.locator('#conversation')).toHaveText('Recovered fixture conversation');
    await expect(page.locator('#conversation')).toHaveAttribute('data-session', 'fixture-conversation');
    await expect(draft).toHaveValue('Unsent local acceptance draft');
    assert.equal(await page.locator('#attachment').evaluate(input => input.files[0].name), 'unsent.txt');
    assert.equal(page.url(), originalUrl);
    await page.screenshot({ path: path.join(evidence, 'access-recovered.png') });
    await signInTab.close();
    accessGate = false;
    // Reload/reopen acceptance is separate from in-memory draft retention.
    available = false;
    await page.reload();
    await expect(page.locator('h1')).toHaveText('Connection to Von interrupted');
    await expect(page.locator('#checked')).toContainText('Last checked');
    await context.setOffline(true);
    await page.locator('#retry').click();
    await expect(page.locator('#connection')).toContainText('device reports that it is offline');
    const reopened = await context.newPage();
    await reopened.goto(`${base}/von/`);
    await expect(reopened.locator('h1')).toHaveText('Connection to Von interrupted');
    const cached = await page.evaluate(async () => {
      const entries = [];
      for (const name of await caches.keys()) for (const request of await (await caches.open(name)).keys()) {
        const response = await (await caches.open(name)).match(request);
        if (response.headers.has('X-Fixture-Private-Header')) throw new Error('Non-public response header cached');
        entries.push(new URL(request.url).pathname);
      }
      return entries.sort();
    });
    assert.deepEqual(cached, ['/static/outage/health.js','/static/outage/offline.html','/static/outage/offline.js','/static/outage/outage.css','/static/outage/status.js']);
    available = true;
    accessGate = true;
    await context.clearCookies();
    await context.setOffline(false);
    await page.locator('#retry').click();
    await expect(page.locator('#connection')).toContainText('may need to sign in again');
    const shellSignInPromise = context.waitForEvent('page');
    await page.getByRole('link', { name: 'Sign in again in a new tab' }).click();
    const shellSignIn = await shellSignInPromise;
    await shellSignIn.getByRole('link', { name: 'Continue with fixture account' }).click();
    await expect(shellSignIn.locator('#promptInput')).toBeVisible();
    await page.bringToFront();
    if (await page.locator('#retry').isVisible()) await page.locator('#retry').click();
    await expect(page.locator('#promptInput')).toBeVisible();
    await page.locator('#vonRecoveredDraft summary').click();
    await expect(page.getByLabel('Recovered unsent draft')).toHaveValue('Unsent local acceptance draft');
    await expect(page.locator('#promptInput')).toHaveValue('');
    assert.equal(effects, 0);
    const result = { surface: 'component/network fixture with cross-origin sign-in and isolated cookie', baselineFailure, opaqueRedirectDetected: true, signInNewTab: true, offlineShellSignIn: true, attachmentRetained: true, conversationRetained: true, viewports: 4, workerLifecycle, transientNoOverlay: true, thinkingNoOverlay: true,
      openDraftRetained: true, reloadDraftRecovery: true, focusRestored: true, offlineReloadAndReopen: true, reconnect: true, cachedPaths: cached, effects };
    fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); await new Promise(resolve => signInServer.close(resolve)); }
})().catch(error => { console.error(error); server.close(); signInServer.close(); process.exitCode = 1; });
