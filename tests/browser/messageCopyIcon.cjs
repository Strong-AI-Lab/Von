// Actual Messages renderer and CSS; synthetic HTTP data, no live state changes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/message-copy';
fs.mkdirSync(evidence, { recursive: true });
const content = '**Research update**\nCopy the original Markdown.';
const server = http.createServer((req, res) => {
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
    if (req.url === '/api/messages/exchange') return res.end(JSON.stringify({ current_user_id: '#V#alice', messages: ['alice', 'bob'].map(sender => ({
        concept_id: `#V#message_${sender}`, created_at: '2026-09-14T12:23:00Z',
        relationships: { '#V#has_sender': [`#V#${sender}`] },
        concept_data: { content_fallback: content, read_by: ['#V#alice'] }
    })) }));
    res.end(JSON.stringify({ success: true, profiles: [], messages: [] }));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [320, 1280]) {
            const page = await browser.newPage({ viewport: { width, height: 850 }, hasTouch: width < 800 });
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                window.IntersectionObserver = undefined;
                window.copied = [];
                Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => window.copied.push(text) } });
                const { openMessageExchange } = await import('/static/js/components/messagePanel.js');
                await openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'], session_name: 'Copy icon fixture' });
            });
            const buttons = page.getByRole('button', { name: 'Copy message as Markdown', exact: true });
            await expect(buttons).toHaveCount(2);
            for (const button of await buttons.all()) {
                await expect(button).toHaveAttribute('title', 'Copy message as Markdown');
                await expect(button.locator('svg')).toBeVisible();
                if (width < 800) await expect(button.locator('.message-copy-label')).toBeVisible();
                else await expect(button.locator('.message-copy-label')).toBeHidden();
                const box = await button.boundingBox();
                assert(box.width >= (width < 800 ? 44 : 32) && box.height >= (width < 800 ? 44 : 32));
            }
            await buttons.first().focus();
            assert.notEqual(await buttons.first().evaluate(el => getComputedStyle(el).outlineStyle), 'none');
            await page.screenshot({ path: path.join(evidence, `copy-${width}.png`) });
            await page.keyboard.press('Enter');
            await expect(page.locator('.message-copy-status').first()).toHaveText('Copied');
            await expect(buttons.first()).toBeFocused();
            assert.equal(await page.evaluate(() => window.copied[0]), content);
            await page.keyboard.press('Space');
            await expect.poll(() => page.evaluate(() => window.copied.length)).toBe(2);
            // Force fallback, checking actual selected text and restored focus.
            await page.evaluate(() => {
                navigator.clipboard.writeText = async () => { throw new Error('Clipboard unavailable'); };
                document.execCommand = () => { window.fallbackText = document.activeElement.value; return true; };
            });
            await page.keyboard.press('Enter');
            await expect.poll(() => page.evaluate(() => window.fallbackText)).toBe(content);
            await expect(buttons.first()).toBeFocused();
            await page.evaluate(() => { document.execCommand = () => false; });
            await page.keyboard.press('Enter');
            await expect(page.locator('.message-copy-status').first()).toHaveText('Copy failed');
            await expect(buttons.first()).toBeFocused();
            await expect(buttons.first().locator('svg')).toBeVisible();
            await expect(page.getByRole('button', { name: 'Discuss with Von', exact: true })).toHaveCount(2);
            assert.deepEqual(errors, []);
            results.push({ width, sentAndReceived: true, keyboardCopy: true, rawMarkdown: true, fallbackFocus: true, visibleFailure: true, touchTextFallback: width < 800 });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
