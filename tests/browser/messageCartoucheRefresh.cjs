// Production Messages renderer/CSS with delayed synthetic HTTP data; no live effects.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/message-cartouche-refresh';
fs.mkdirSync(evidence, { recursive: true });
let revision = 0;
const message = (id, date, content) => ({ concept_id: id, created_at: date,
    relationships: { '#V#has_sender': ['#V#bob'] },
    concept_data: { content_fallback: content, subject: revision === 2 ? 'Updated subject' : '' } });
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
        const { before } = JSON.parse(body);
        const messages = before ? [message('#V#older', '2026-09-13T12:00:00Z', 'Earlier #V#older_concept')]
            : [message('#V#recent', '2026-09-14T12:00:00Z', 'Task: #V#research_task')];
        if (!before && revision === 1) messages.push(message('#V#new', '2026-09-15T12:00:00Z', 'New #V#new_concept'));
        return setTimeout(() => res.end(JSON.stringify({ current_user_id: '#V#alice', messages, before: before ? null : 'earlier' })), 100);
    }
    if (req.url.startsWith('/vontology/api/vontology/node_content')) {
        return setTimeout(() => res.end(JSON.stringify({ display_name: 'Research task', kind: 'individual', raw_doc: { names: [{ name: 'Research task', language: 'en-NZ', type: 'NL' }] } })), 250);
    }
    res.end(JSON.stringify({ success: true, profiles: [], messages: [] }));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [390, 1280]) {
            revision = 0;
            const page = await browser.newPage({ viewport: { width, height: 850 } });
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                window.IntersectionObserver = undefined;
                window.panel = await import('/static/js/components/messagePanel.js');
                await window.panel.openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'], session_name: 'Cartouche fixture' });
                window.original = document.querySelector('.vontology-cartouche');
                window.bubble = window.original.closest('.message-bubble');
                window.removals = 0;
                window.observer = new MutationObserver(records => {
                    for (const record of records) for (const node of record.removedNodes) {
                        if (node === window.original || node.contains?.(window.original)) window.removals++;
                    }
                });
                window.observer.observe(document.querySelector('#messageViewContent'), { childList: true, subtree: true });
            });
            await expect(page.locator('.vontology-cartouche-name')).toHaveText('Research task');
            await page.getByRole('button', { name: 'Load earlier messages', exact: true }).click();
            await expect(page.locator('.message-bubble')).toHaveCount(2);
            await expect.poll(() => page.evaluate(() => document.querySelector('#messageViewContent').textContent.includes('…'))).toBe(false);
            const pagination = await page.evaluate(() => ({ connected: window.original.isConnected, removals: window.removals, text: window.original.textContent }));
            assert.equal(pagination.connected, true, 'pagination retains hydrated cartouche node');
            assert.equal(pagination.removals, 0, 'pagination must not detach the existing cartouche');
            revision = 1;
            await page.evaluate(() => window.panel.refreshOpenMessageExchange());
            await expect(page.locator('.message-bubble')).toHaveCount(3);
            assert.deepEqual(await page.evaluate(() => ({ connected: window.original.isConnected, removals: window.removals })), { connected: true, removals: 0 });
            await page.screenshot({ path: path.join(evidence, `stable-${width}.png`) });
            // Same body but changed metadata must update; deleted recent rows disappear.
            revision = 2;
            await page.evaluate(() => window.panel.refreshOpenMessageExchange());
            await expect(page.locator('[data-contribution-id="#V#recent"] .message-subject')).toHaveText('Updated subject');
            await expect(page.locator('[data-contribution-id="#V#new"]')).toHaveCount(0);
            assert.deepEqual(errors, []);
            results.push({ width, pagination, refreshRetainsNode: true, metadataEditAndDeletion: true, errors });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ fixture: true, authenticatedLiveAcceptance: false, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
