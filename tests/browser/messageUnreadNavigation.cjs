// Production message pane and CSS with synthetic paginated HTTP responses.
// No live authentication, messages or read-state writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/unread-navigation';
fs.mkdirSync(evidence, { recursive: true });
let cursors = [];
const message = (id, unread, day) => ({ concept_id: id, created_at: `2026-09-${day}T12:00:00Z`,
    relationships: { '#V#has_sender': ['#V#bob'], '#V#has_recipient': ['#V#alice'] },
    concept_data: { content_fallback: id, read_by: unread ? [] : ['#V#alice'] } });
const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><div id="conversationWorkspace" class="show-message-exchange"><div id="messagesContainer"></div></div>');
    }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
            return res.end(fs.readFileSync(file));
        }
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/messages/exchange') {
        let body = ''; for await (const chunk of req) body += chunk;
        const { before } = JSON.parse(body); cursors.push(before);
        const result = !before ? { messages: [message('Latest read message', false, '13')], before: 'middle' }
            : before === 'middle' ? { messages: [message('Middle read message', false, '12')], before: 'older' }
                : { messages: [message('Earlier unread', true, '10'), message('Most recent unread', true, '11')], before: null };
        return res.end(JSON.stringify({ ...result, current_user_id: '#V#alice' }));
    }
    res.end(JSON.stringify({ success: true, messages: [], profiles: [] }));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [390, 1280]) {
            cursors = [];
            const page = await browser.newPage({ viewport: { width, height: 850 } });
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                // Keep read markers stable while checking navigation independently.
                window.IntersectionObserver = undefined;
                const { openMessageExchange } = await import('/static/js/components/messagePanel.js');
                await openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice',
                    participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'],
                    session_name: 'Unread pagination fixture', shared_unread_count: 2 });
            });
            await page.getByRole('button', { name: 'Jump to most recent unread', exact: true }).click();
            const target = page.locator('[data-contribution-id="Most recent unread"]');
            await expect(target).toBeFocused();
            await expect(target).toBeInViewport();
            assert.deepEqual(cursors, [null, 'middle', 'older']);
            assert.deepEqual(errors, []);
            await page.screenshot({ path: path.join(evidence, `unread-${width}.png`) });
            results.push({ width, cursors: [...cursors], mostRecentUnreadFocused: true, targetInViewport: true });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
