// Actual Settings template/modules with synthetic API state; no live service or model calls.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/settingsModelPreference.cjs EVIDENCE_DIR
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '/tmp/settings-model-preference';
const html = fs.readFileSync(path.join(root, 'templates/settings_tab.html'), 'utf8')
    .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g, '/static/$1');
const luna = { provider: 'openai', model: 'gpt-5.6-luna' };
const astra = { provider: 'openai', model: 'gpt-6-astra' };
const ollama = { provider: 'ollama', model: 'gemma4:latest', host: 'http://127.0.0.1:11434' };
let state = { openai_api_key_env_var: 'OPENAI_API_KEY', enabled_llms: [luna, astra, ollama],
    resolved_llm: { ...luna, scope: 'user' }, active_llm: luna };
const writes = [];
let probeUsable = true;
const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); return; }
    if (url.pathname.startsWith('/static/')) {
        const file = path.resolve(root, '.' + url.pathname);
        if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file)) {
            res.writeHead(404); res.end(); return;
        }
        res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
        res.end(fs.readFileSync(file)); return;
    }
    let body = '';
    for await (const chunk of req) body += chunk;
    const payload = body ? JSON.parse(body) : {};
    let result = {};
    const target = url.pathname;
    if (target === '/api/settings/') {
        if (req.method === 'POST') {
            writes.push(payload);
            state = { ...state, ...payload, resolved_llm: { ...(payload.active_llm || state.resolved_llm), scope: 'user' } };
        }
        result = { ...state, success: true };
    } else if (target === '/von/api/auth/status') {
        result = { authenticated: true, user_concept_id: '#V#model-fixture-user', name: 'Model Fixture', email: 'fixture@example.invalid' };
    } else if (target === '/von/api/session/context' || target === '/von/api/session/set_organisation') {
        result = { status: 'updated', role: 'user', organisation_id: null, namespace: '#V#model-fixture-user' };
    } else if (target.includes('/model_parameters/capabilities')) {
        result = { success: true, parameters: { reasoning_effort: { supported: true, allowed_values: ['low', 'medium', 'high'] } } };
    } else if (target === '/api/settings/models/openai') {
        result = ['gpt-5.6-luna', 'gpt-6-astra', 'unallowed-draft'];
    } else if (target === '/api/settings/ollama/models') {
        result = { models: ['gemma4:latest'] };
    } else if (target === '/api/settings/ollama/hosts') {
        result = { hosts: [], active_host: null };
    } else if (target === '/api/settings/organisations') {
        result = [];
    } else if (target === '/api/settings/env_var/check') {
        result = { exists: false };
    } else if (target === '/api/settings/openai/test_model') {
        result = { usable: probeUsable, model: payload.model, reason: probeUsable ? 'Synthetic probe succeeded' : 'Provider disabled or model unavailable (fixture)' };
    }
    res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(result));
});
(async () => {
    fs.mkdirSync(evidence, { recursive: true });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    const results = [];
    try {
        const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => {
            if (localStorage.getItem('fixture-initialised')) return;
            localStorage.setItem('fixture-initialised', 'true');
            localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#model-fixture-user', name: 'Model Fixture' }));
            localStorage.setItem('von:localModelPreference', JSON.stringify({ schemaVersion: 'localModelPreference.v1', activeSource: 'openai', openaiModel: 'gpt-6-astra',
                ollamaSelection: { value: 'http://127.0.0.1:11434:gemma4:latest', ...{ model: 'gemma4:latest', host: 'http://127.0.0.1:11434' } } }));
        });
        const preference = () => page.evaluate(() => JSON.parse(localStorage.getItem('von:localModelPreference')));
        const ready = async () => {
            await page.waitForFunction(() => !document.getElementById('saveModelPoolButton')?.disabled);
            await page.locator('[data-settings-concern-tab="models"]').click();
            await page.waitForFunction(() => document.getElementById('openaiModelSelect')?.value === JSON.parse(localStorage.getItem('von:localModelPreference')).openaiModel);
        };
        const check = async (name, model, source = 'openai') => {
            const stored = await preference();
            assert.equal(stored.activeSource, source, name);
            assert.equal(stored.openaiModel, model, name);
            results.push({ name, model, source });
        };
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        await ready();
        assert.equal(await page.locator('#enableOpenAiPremiumToggle').isChecked(), true);
        await check('reload prefers browser Astra over server Luna', 'gpt-6-astra');
        await page.selectOption('#openaiModelSelect', 'gpt-5.6-luna');
        await page.waitForFunction(() => document.getElementById('enableOpenAiPremiumToggle').checked);
        await check('select already allowed Luna', 'gpt-5.6-luna');
        assert.equal(writes.length, 0, 'selection must not write allow list');
        await page.selectOption('#openaiReasoningEffortSelect', 'high');
        await check('edit reasoning retains browser choice', 'gpt-5.6-luna');
        assert.equal(writes.length, 0);
        await page.locator('#useBrowserModelAsScopedPrimaryButton').click();
        await page.locator('#saveModelPoolButton').click();
        await page.waitForFunction(() => document.getElementById('modelPoolStatusMessage').textContent.includes('saved'));
        assert.equal(writes.length, 1, 'only explicit scoped save writes allowance');
        await check('explicit edit and scoped save retain browser choice', 'gpt-5.6-luna');
        await page.reload(); await ready();
        await check('reload retains model and parameters', 'gpt-5.6-luna');
        assert.equal((await preference()).openaiModelParameters.reasoning_effort, 'high');
        await page.locator('#enableOpenAiPremiumToggle').uncheck();
        await check('explicit toggle off selects Ollama', 'gpt-5.6-luna', 'ollama');
        await page.locator('#enableOpenAiPremiumToggle').check();
        await check('explicit toggle on restores Luna', 'gpt-5.6-luna');
        await page.selectOption('#openaiModelSelect', 'unallowed-draft');
        await page.waitForFunction(() => document.getElementById('enableOpenAiPremiumToggle').disabled);
        await check('unallowed draft preserves active Luna', 'gpt-5.6-luna');
        assert.equal(await page.locator('#enableOpenAiPremiumToggle').isChecked(), false);
        await page.selectOption('#openaiModelSelect', 'gpt-6-astra');
        await check('another allowed selection switches to Astra', 'gpt-6-astra');
        await page.reload(); await ready();
        await check('second allowed choice survives reload', 'gpt-6-astra');
        probeUsable = false;
        await page.locator('#testOpenAiModelButton').click();
        await page.waitForFunction(() => document.getElementById('openaiModelStatusMessage').textContent.includes('Restore provider/model availability'));
        await check('failed availability probe retains explicit choice', 'gpt-6-astra');
        state.enabled_llms = [luna, ollama];
        await page.reload(); await ready();
        await check('scope removal retains choice without fallback', 'gpt-6-astra');
        assert.match(await page.locator('#browserChatModelSummary').textContent(), /select an allowed replacement explicitly/);
        assert.match(await page.locator('#settingsConcernSummary').textContent(), /Choose an allowed browser model or restore its allowance/);
        assert.equal(await page.locator('#enableOpenAiPremiumToggle').isDisabled(), true);
        await page.screenshot({ animations: 'disabled', path: path.join(evidence, 'invalidated-choice.png') });
        await page.selectOption('#openaiModelSelect', 'gpt-5.6-luna');
        await check('explicit replacement recovers', 'gpt-5.6-luna');
        assert.equal(writes.length, 1);
        await page.screenshot({ animations: 'disabled', path: path.join(evidence, 'recovered-choice.png') });
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify({ fixture: 'actual candidate template/modules; synthetic API; no authentication or live writes', sourceSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'static/js/settingsPage.js'))).digest('hex'), results, writes }, null, 2));
        console.log(`${results.length} Settings browser fixture checks passed; ${evidence}`);
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
