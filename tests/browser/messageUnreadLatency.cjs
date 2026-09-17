// Production pane with a deterministic 1,000-message HTTP fixture. No live data.
// Optional baseline module path lets the same workload measure an older revision.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/unread-latency';
const baseline = process.argv[3];
fs.mkdirSync(evidence, { recursive: true });
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8').replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
const messages = Array.from({ length: 1000 }, (_, i) => ({
    concept_id: `message-${String(i).padStart(4, '0')}`,
    created_at: new Date(Date.UTC(2026, 8, 1) + i * 1000).toISOString(),
    relationships: { '#V#has_sender': ['#V#bob'], '#V#has_recipient': ['#V#alice'] },
    concept_data: { content_fallback: `Message ${i}. ${'Research progress and follow-up. '.repeat(8)}`, read_by: [700, 980].includes(i) ? [] : ['#V#alice'] }
}));
let requests = 0;
const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end(`<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css">${markup}`);
    }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
            return res.end(fs.readFileSync(baseline && req.url.endsWith('/components/messagePanel.js') ? baseline : file));
        }
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/messages/exchange') {
        let body = ''; for await (const chunk of req) body += chunk;
        const { before, after, first_unread } = JSON.parse(body);
        requests++;
        const start = first_unread ? 700 : after ? Number(after) : Math.max(0, (before ? Number(before) : 1000) - 50);
        const end = Math.min(start + 50, 1000);
        await new Promise(resolve => setTimeout(resolve, 50));
        return res.end(JSON.stringify({ messages: messages.slice(start, end), before: start || null,
            after: (first_unread || after) && end < 1000 ? end : null, current_user_id: '#V#alice' }));
    }
    res.end(JSON.stringify({ success: true, profiles: [], messages: [] }));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [390, 1280]) {
            const page = await browser.newPage({ viewport: { width, height: 850 } });
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                window.IntersectionObserver = undefined;
                document.getElementById('conversationWorkspace').classList.add('show-message-exchange');
                document.body.classList.add('viewing-message-exchange');
                const panel = await import('/static/js/components/messagePanel.js');
                await panel.openMessageExchange({ session_id: 'latency', viewer_id: '#V#alice',
                    participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'],
                    shared_unread_count: 1 }); // Deliberately stale; earlier unread must win.
            });
            requests = 0;
            const ms = await page.evaluate(async () => {
                const started = performance.now();
                document.querySelector('.message-jump-unread').click();
                while (document.activeElement?.dataset.contributionId !== 'message-0700') {
                    if (performance.now() - started > 60000) throw new Error('Unread navigation did not finish');
                    await new Promise(resolve => requestAnimationFrame(resolve));
                }
                return performance.now() - started;
            });
            const rendered = await page.locator('[data-contribution-id]').count();
            assert.deepEqual(errors, []);
            if (!baseline) { assert.equal(requests, 1); assert.equal(rendered, 50); assert.ok(ms < 1000, `${ms}ms exceeds fixture target`); }
            results.push({ width, milliseconds: ms, exchangeRequests: requests, renderedMessages: rendered });
            await page.screenshot({ path: path.join(evidence, `unread-${width}.png`) });
            await page.close();
        }
        const result = { fixture: true, messageCount: 1000, responseDelayMs: 50, baseline: Boolean(baseline), results };
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
        console.log(JSON.stringify(result));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
