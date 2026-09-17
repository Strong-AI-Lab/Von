// Candidate markup/module with synthetic model capabilities; no model calls or live writes.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/turnModelPicker.cjs
const fs = require('fs');
const http = require('http');
const path = require('path');
const assert = require('assert/strict');
const { chromium } = require('playwright');
const root = process.cwd();
const evidence = process.env.VON_PICKER_EVIDENCE_DIR || '/tmp/von-turn-picker';
const staticRoot = path.join(root, 'src/frontend/web/von_interface/static');
const template = fs.readFileSync(path.join(root, 'src/frontend/web/von_interface/templates/chat_tab.html'), 'utf8');
const section = template.match(/<section class="composer-options-section" aria-labelledby="composerModelHeading">[\s\S]*?<\/section>/)[0];
const server = http.createServer((req, res) => {
    if (req.url.startsWith('/static/')) {
        const file = path.join(staticRoot, req.url.slice('/static/'.length));
        res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
        return res.end(fs.readFileSync(file));
    }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<html><body>${section}<p id="turnModelStatus"></p><button id="clearTurnModelButton">Clear</button>
    <button id="sendFixture">Capture request</button><script type="module">
    import { createTurnModelPicker } from '/static/js/components/turnModelPicker.js';
    window.picker = createTurnModelPicker({
        select: document.getElementById('turnModelSelect'), status: document.getElementById('turnModelStatus'),
        clearButton: document.getElementById('clearTurnModelButton'), reasoningSelect: document.getElementById('turnReasoningSelect'),
        loadModels: async provider => provider === 'openai' ? ['gpt-6-astra', 'plain-model'] : [],
        loadCapabilities: async (provider, model) => ({ parameters: { reasoning_effort: {
            supported: model === 'gpt-6-astra', allowed_values: ['low', 'medium', 'high']
        } } })
    });
    document.getElementById('sendFixture').onclick = () => { window.captured = window.picker.take(); };
    await window.picker.load(); window.ready = true;
    </script></body></html>`);
});
(async () => {
    let browser;
    try {
        fs.mkdirSync(evidence, { recursive: true });
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.waitForFunction(() => window.ready);
        await page.locator('#turnModelSelect').selectOption({ label: 'gpt-6-astra' });
        await page.locator('#turnReasoningSelect').selectOption('high');
        await page.screenshot({ path: path.join(evidence, 'reasoning-selected.png') });
        await page.locator('#sendFixture').click();
        assert.deepEqual(await page.evaluate(() => window.captured), {
            model: 'gpt-6-astra', model_provider: 'openai', model_parameters: { reasoning_effort: 'high' }
        });
        assert.equal(await page.locator('#turnModelSelect').inputValue(), '');
        assert.equal(await page.locator('#turnReasoningSelect').isDisabled(), true);
        await page.locator('#turnModelSelect').selectOption({ label: 'plain-model' });
        await page.waitForFunction(() => document.getElementById('turnReasoningSelect').textContent.includes('unavailable'));
        assert.equal(await page.locator('#turnReasoningSelect').isDisabled(), true);
        await page.evaluate(() => window.picker.invalidate());
        assert.equal(await page.locator('#turnModelSelect option').count(), 1);
        console.log(JSON.stringify({ passed: true, scope: 'candidate template and picker module; synthetic catalogue and registry; no live authentication or provider calls', evidence }));
    } finally {
        if (browser) await browser.close();
        server.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
