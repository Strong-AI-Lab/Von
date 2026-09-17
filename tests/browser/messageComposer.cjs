// Fixture-backed browser check of the production Messages module and CSS.
// No live accounts, backend writes or model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');

const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const markup = '<div id=messagesContainer class=unified-message-content></div>';
const evidence = process.argv[2];
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/static/styles.css">${markup}`);
        return;
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) {
        res.writeHead(404).end();
        return;
    }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        for (const width of [1280, 390]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            let sent;
            let messages = [{ concept_id: '#V#source', concept_data: { content_fallback: 'Please review the experiment results.' }, relationships: { '#V#has_sender': ['#V#bob'] } }];
            await page.route('**/api/**', async route => {
                const url = route.request().url();
                let data = {};
                if (url.endsWith('/messages/') && route.request().method() === 'POST') {
                    sent = route.request().postDataJSON();
                    messages = [{ concept_id: '#V#reply', concept_data: { content_fallback: sent.content }, relationships: { '#V#has_sender': ['#V#alice'] } }];
                    data = { success: true };
                } else if (url.includes('/messages/exchange')) data = { messages, current_user_id: '#V#alice' };
                else if (url.includes('/vontology/search')) data = { results: [{ id: '#V#research', name: 'Research', kind: 'type' }] };
                else if (url.includes('/messages/threads')) data = { threads: [] };
                await route.fulfill({ json: data });
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                const panel = await import('/static/js/components/messagePanel.js');
                await panel.openMessageExchange({ session_id: 'existing', viewer_id: '#V#alice', session_name: 'Bob',
                    participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'], organisation_concept_id: '#V#lab' });
            });
            const input = page.locator('#messageInput');
            await input.fill('#V#res');
            await expect(page.locator('.concept-autocomplete-item').first()).toBeVisible();
            await input.press('ArrowDown');
            await input.press('Enter');
            assert.equal(sent, undefined, 'Autocomplete Enter must not send');
            await expect(input).toHaveValue(/research/);
            await expect(page.locator('#messageComposeArea .prompt-vontology-cartouche').first()).toBeVisible();
            if (width > 800) await expect(page.locator('#messageComposeArea button[aria-label="Dictate"]')).toBeVisible();
            await expect(page.locator('#messageComposeArea button[aria-label="Attach files"]')).toBeVisible();
            const more = await page.locator('#messageComposeArea summary').boundingBox();
            const composer = await page.locator('#messageComposeArea').boundingBox();
            assert.ok(more.y >= composer.y && more.y + more.height <= composer.y + composer.height, 'More actions stays inside the composer');
            await page.locator('#messageComposeArea summary').click();
            await expect(page.locator('#messageComposeArea select').last()).toBeVisible();
            await expect(page.locator('#messageComposeArea button[aria-label="Dictate"]')).toBeVisible();
            await page.locator('#messageComposeArea summary').press('Escape');
            const dimensions = await input.evaluate(el => ({ width: el.getBoundingClientRect().width, page: document.documentElement.scrollWidth, viewport: innerWidth }));
            assert.ok(dimensions.width > 150, JSON.stringify(dimensions));
            assert.ok(dimensions.page <= dimensions.viewport, JSON.stringify(dimensions));
            if (evidence) await page.screenshot({ path: path.join(evidence, `message-composer-${width}.png`) });
            if (width < 800) {
                await input.press('Enter');
                assert.equal(sent, undefined, 'Mobile Enter inserts a newline');
            }
            await page.locator('#sendMessageBtn').click();
            await expect(input).toHaveValue('');
            assert.equal(sent.content, '#V#research');
            assert.deepEqual(sent.recipient_ids, ['#V#bob']);
            assert.equal(sent.organisation_concept_id, '#V#lab');
            // New-message content uses precisely the same editor and controls.
            await page.locator('#newMessageBtn').click();
            await page.locator('#newMessageRecipient').fill('#V#bob');
            const draft = page.locator('#newMessageContent');
            await draft.fill('#V#res');
            await expect(page.locator('.concept-autocomplete-item').first()).toBeVisible();
            await draft.press('ArrowDown'); await draft.press('Enter');
            await expect(page.locator('.message-modal-body .prompt-vontology-cartouche').first()).toBeVisible();
            await page.locator('#sendNewMessage').click();
            await expect(page.locator('#newMessageModal')).toHaveClass(/hidden/);
            assert.equal(sent.content, '#V#research');
            await page.close();
        }
        console.log('PASS: production Messaging reply/new-message autocomplete, cartouches, controls, normalised sends, keyboard and layout at 1280 and 390px (fixture APIs).');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
