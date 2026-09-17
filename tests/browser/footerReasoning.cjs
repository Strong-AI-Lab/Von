// Isolated browser acceptance: real Settings/footer modules and template, fixture APIs.
// No live identity, database changes, or provider calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '/tmp/footer-reasoning';
fs.mkdirSync(evidence, { recursive: true });
const model = 'gpt-5.6-luna';
const llm = { provider: 'openai', model, model_parameters: { reasoning_effort: 'low' } };
const requests = [];
const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
        const html = fs.readFileSync(path.join(root, 'templates/settings_tab.html'), 'utf8')
            .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
            .replace(/{{ url_for\('static', filename='([^']+)'\) }}/g, '/static/$1')
            .replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
        res.setHeader('Content-Type', 'text/html');
        return res.end(html.replace('</body>', '<div class="footer-container"><p id="modelInfoFooter"></p></div></body>'));
    }
    if (pathname.startsWith('/api/') || pathname.startsWith('/von/api/')) {
        res.setHeader('Content-Type', 'application/json');
        let payload = {};
        if (pathname === '/api/settings/') payload = {
            active_llm: llm, resolved_llm: llm, enabled_llms: [llm],
            openai_api_key_env_var: 'OPENAI_API_KEY', server_default_llm: llm,
        };
        if (pathname.includes('model_parameters/capabilities')) payload = {
            success: true, parameters: { reasoning_effort: { supported: true, allowed_values: ['low', 'medium', 'high'] } },
        };
        if (pathname.includes('auth/status')) payload = { authenticated: true, user_concept_id: '#V#fixture', name: 'Fixture' };
        if (pathname.includes('session/context')) payload = { authenticated: true, context_source: 'window_session', user_id: '#V#fixture', role: 'user', namespace: null };
        if (pathname.includes('llm/info')) payload = { ...llm, status: 'ready' };
        if (pathname.includes('capability-index/status')) payload = { ready: true, status: 'ready' };
        if (pathname.endsWith('/models/openai')) payload = [model];
        if (pathname.includes('organisations')) payload = [];
        if (pathname.endsWith('/ollama/models')) payload = { models: [] };
        if (pathname.endsWith('/ollama/hosts')) payload = { hosts: [] };
        if (pathname.endsWith('/openai/test_model')) {
            let body = '';
            req.on('data', chunk => { body += chunk; });
            return req.on('end', () => {
                requests.push(JSON.parse(body));
                res.end(JSON.stringify({ usable: true, model, reason: 'fixture' }));
            });
        }
        return res.end(JSON.stringify(payload));
    }
    const file = path.resolve(root, `.${pathname}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    console.log('Fixture listening');
    const browser = await chromium.launch({ headless: true, timeout: 20000 });
    console.log('Browser launched');
    try {
        const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
        page.on('pageerror', error => console.error('Page error:', error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        console.log('Template loaded');
        await page.evaluate(({ model }) => {
            localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#fixture', name: 'Fixture' }));
            localStorage.setItem('von:localModelPreference', JSON.stringify({
                activeSource: 'openai', openaiModel: model, openaiModelParameters: { reasoning_effort: 'low' },
            }));
        }, { model });
        async function mount() {
            await page.evaluate(async () => {
                const footer = await import('/static/js/domUtils.js');
                window.updateModelInfoFooterDisplay = footer.setModelInfoFooterText;
                await import('/static/js/settingsPage.js');
                document.dispatchEvent(new Event('DOMContentLoaded'));
                await footer.setModelInfoFooterText();
            });
            await page.locator('[data-settings-concern-tab="models"]').click();
        }
        await mount();
        console.log('Mounted');
        const select = page.locator('#openaiReasoningEffortSelect');
        await expect(select).toHaveValue('low');
        await expect(page.locator('.footer-reasoning-level')).toHaveText('Reasoning: low');
        await select.selectOption('high');
        await expect(page.locator('.footer-reasoning-level')).toHaveText('Reasoning: high');
        await page.reload();
        await mount();
        console.log('Mounted');
        await expect(select).toHaveValue('high');
        await expect(page.locator('.footer-reasoning-level')).toHaveText('Reasoning: high');
        await page.locator('#testOpenAiModelButton').click();
        await expect.poll(() => requests.length).toBe(1);
        assert.equal(requests[0].model_parameters.reasoning_effort, 'high');
        const requestFields = await page.evaluate(async () => (await import('/static/js/utils/localModelPreferences.js')).buildLocalModelRequestFields());
        assert.equal(requestFields.model_parameters.reasoning_effort, 'high');
        const measurements = [];
        for (const width of [1280, 390]) {
            await page.setViewportSize({ width, height: 900 });
            const label = page.locator('.footer-reasoning-level .footer-label-inline');
            assert.ok((await label.boundingBox()).width > 20, 'Reasoning label remains visible when compact');
            await page.locator('.footer-reasoning-level').screenshot({ path: path.join(evidence, `reasoning-${width}.png`) });
            measurements.push({ width, label: await label.boundingBox() });
        }
        await select.selectOption('');
        await expect(page.locator('.footer-reasoning-level')).toHaveText('Reasoning: provider default');
        assert.equal(await page.evaluate(async () => (await import('/static/js/utils/localModelPreferences.js')).buildLocalModelRequestFields().model_parameters), undefined);
        fs.writeFileSync(path.join(evidence, 'receipt.json'), JSON.stringify({ mode: 'isolated fixture APIs; real template and modules', requests, requestFields, measurements, reload: 'high restored', default: 'override omitted' }, null, 2));
        console.log('PASS: Settings → persisted preference → footer → reload → requested effort; desktop/mobile labels; provider default.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
