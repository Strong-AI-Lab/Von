const fs = require('fs');
const http = require('http');
const path = require('path');
// Actual Settings markup and modules against isolated synthetic HTTP state.
// Run: PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/modelEnablement.cjs
const root = process.cwd();
fs.mkdirSync('/tmp/von-model-browser', { recursive: true });
const { chromium } = require(path.join(root, 'node_modules/playwright'));
let stored = { resolved_llm: { provider: 'ollama', model: 'local-model', host: 'http://localhost:11434', scope: 'user' }, enabled_llms: [{ provider: 'ollama', model: 'local-model', host: 'http://localhost:11434' }] };
const audio = { provider: 'openai', model: 'gpt-4o-transcribe', available: true, audio_transcription: true };
let saveCount = 0;
let failSave = false;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    let html = fs.readFileSync(path.join(root, 'src/frontend/web/von_interface/templates/settings_tab.html'), 'utf8');
    html = html.replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g, '/static/$1');
    html = html.replace(/<script[\s\S]*?<\/script>/g, '');
    res.setHeader('Content-Type', 'text/html'); return res.end(html);
  }
  if (url.pathname.startsWith('/static/')) {
    const file = path.join(root, 'src/frontend/web/von_interface', url.pathname);
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/plain');
    if (fs.existsSync(file)) return res.end(fs.readFileSync(file));
    res.statusCode = 404; return res.end();
  }
  res.setHeader('Content-Type', 'application/json');
  if (url.pathname.includes('/models/inventory/')) {
    const provider = url.pathname.split('/').pop();
    const models = provider === 'openai' ? [audio, { provider, model: 'general-chat', available: true }] : provider === 'ollama' ? [{ ...stored.resolved_llm, available: true }] : provider === 'openrouter' ? [{ provider, model: 'vendor/chat', available: true }] : [];
    return res.end(JSON.stringify({ provider, available: true, models }));
  }
  if (url.pathname === '/api/settings/') {
    if (req.method === 'POST') {
      saveCount++;
      let body = ''; for await (const chunk of req) body += chunk;
      if (failSave) { res.statusCode = 500; return res.end(JSON.stringify({ message: 'Fixture persistence temporarily unavailable; retry.' })); }
      const data = JSON.parse(body);
      stored = { resolved_llm: data.active_llm, enabled_llms: data.enabled_llms };
    }
    return res.end(JSON.stringify(stored));
  }
  return res.end('{}');
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(url);
  await page.evaluate(async (stored) => {
    sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#fixture-user' }));
    const mod = await import('/static/js/settingsPage.js');
    window.settingsFixture = mod;
    mod.__testOnly_setModelScopeState({ resolvedLlm: stored.resolved_llm, enabledLlms: stored.enabled_llms, actorReady: true });
    mod.__testOnly_initialiseSettingsConcernNavigation();
    mod.__testOnly_renderModelScopeOverview();
    await mod.__testOnly_refreshAdditionalModelInventory();
  }, stored);
  await page.locator('[data-settings-concern-tab="models"]').click();
  await page.locator('summary').filter({ hasText: 'Additional models' }).click();
  const row = page.locator('#additionalModelInventory .settings-model-pool-entry').filter({ hasText: 'openai: gpt-4o-transcribe' });
  failSave = true;
  await row.locator('button').click();
  await page.waitForFunction(() => document.getElementById('modelPoolStatusMessage').textContent.includes('temporarily unavailable'));
  failSave = false;
  await row.locator('button').click();
  await page.waitForFunction(() => document.getElementById('modelPoolStatusMessage').textContent.includes('saved for the current scope'));
  if (saveCount !== 2 || stored.enabled_llms.length !== 2 || stored.resolved_llm.model !== 'local-model') throw Error('Save/recovery or preservation failed');
  await page.locator('#reloadModelPoolButton').click();
  await page.waitForFunction(() => document.getElementById('modelPoolStatusMessage').textContent.includes('Model settings loaded'));
  const select = page.locator('#settingsTranscriptionModelSelect');
  await page.locator('[data-settings-concern-tab="conversations"]').click();
  await select.selectOption('gpt-4o-transcribe');
  await select.scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/von-model-browser/transcription-desktop.png' });
  const result = await page.evaluate(() => ({
    selected: localStorage.getItem('chatRecordedTranscriptionModel'),
    options: [...document.querySelector('#settingsTranscriptionModelSelect').options].map(o => ({ value: o.value, disabled: o.disabled })),
    inventory: document.getElementById('additionalModelInventory').textContent,
    overflow: document.documentElement.scrollWidth > innerWidth,
  }));
  if (result.options.some(o => o.value === 'general-chat') || result.selected !== 'gpt-4o-transcribe') throw Error('Filtering/selection failed');
  await page.locator('[data-settings-concern-tab="models"]').click();
  await page.screenshot({ path: '/tmp/von-model-browser/desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '/tmp/von-model-browser/mobile.png', fullPage: true });
  await page.locator('[data-settings-concern-tab="conversations"]').click();
  await select.scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/tmp/von-model-browser/transcription-mobile.png' });
  result.mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  fs.writeFileSync('/tmp/von-model-browser/evidence.json', JSON.stringify({ url, saveCount, result, errors, fixture: 'Actual Settings template/modules; synthetic discovery and canonical settings HTTP responses; no provider generation or live data.' }, null, 2));
  console.log(JSON.stringify({ saveCount, selected: result.selected, options: result.options, overflow: result.overflow, mobileOverflow: result.mobileOverflow, errors }));
  await browser.close(); server.close();
})().catch(error => { console.error(error); server.close(); process.exit(1); });
