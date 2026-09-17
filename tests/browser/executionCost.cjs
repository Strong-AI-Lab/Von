// Local fixture: actual template, stylesheet and chat renderer, no live services.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/executionCost.cjs /tmp/cost-evidence
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '/tmp/execution-cost-evidence';
const template = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8');
const card = template.slice(template.indexOf('    <div class="thinking-card-wrapper"'), template.indexOf('    <section id="chatTaskQueuePanel"'))
    .replaceAll('aria-hidden="true"', 'aria-hidden="false"');
const html = `<!doctype html><link rel="stylesheet" href="/static/styles.css"><main style="padding:12px">${card}</main>
<script type="module">
import { __testOnly_updateThinkingCardMeta } from '/static/js/chatTab.js';
window.showCost = amount => __testOnly_updateThinkingCardMeta({
  thinkingStartedAtMs: 1000, thinkingFinishedAtMs: 71000,
  llmUsageCostSummary: { estimated_cost: amount === null ? null : { status: 'estimated', currency: 'USD', amount_decimal: amount } }
}, { status: 'completed', last_activity_at_utc: new Date().toISOString() });
window.showCost(null);
</script>`;
const server = http.createServer((req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html); return; }
    if (req.url === '/api/settings/execution_cost_display') {
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ currency: 'USD', display_above: '0.01', alert_above: '0.10' })); return;
    }
    const file = path.resolve(root, '.' + req.url.split('?')[0]);
    if (!file.startsWith(path.join(root, 'static') + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        res.writeHead(404); res.end(); return;
    }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
    res.end(fs.readFileSync(file));
});
(async () => {
    fs.mkdirSync(evidence, { recursive: true });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    const results = [];
    try {
        for (const width of [1440, 390]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            await page.goto(`http://127.0.0.1:${server.address().port}/`);
            await page.waitForFunction(() => typeof window.showCost === 'function');
            await page.waitForTimeout(150);
            const initial = await page.locator('#loadingIndicator').boundingBox();
            const initialSlot = await page.locator('.thinking-execution-cost').boundingBox();
            for (const [amount, visible, high] of [[null, false, false], ['0', false, false], ['0.01', false, false], ['0.010001', true, false], ['0.10', true, false], ['0.100001', true, true]]) {
                await page.evaluate(value => window.showCost(value), amount);
                const slot = page.locator('.thinking-execution-cost');
                assert.equal(Boolean(await slot.textContent()), visible);
                assert.equal((await slot.getAttribute('class')).includes('thinking-execution-cost-high'), high);
                if (high) assert.match(await slot.getAttribute('aria-label'), /High cost/);
                if (high) assert.equal(await slot.evaluate(el => getComputedStyle(el).color), 'rgb(185, 28, 28)');
                const box = await page.locator('#loadingIndicator').boundingBox();
                assert.deepEqual(await slot.boundingBox(), initialSlot, `cost slot moved at ${width}/${amount}`);
                assert.equal(box.height, initial.height, `layout shift at ${width}/${amount}`);
                assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
                assert.match(await page.locator('#thinkingCardMeta').textContent(), /^1m 10s/);
                assert.match(await page.locator('#thinkingCardMeta').textContent(), /Last activity/);
                if (high) await page.screenshot({ path: path.join(evidence, `cost-${width}.png`) });
                results.push({ width, amount, visible, high, headerHeight: box.height });
            }
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify(results, null, 2));
        console.log(`${results.length} fixture browser checks passed; ${evidence}`);
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
