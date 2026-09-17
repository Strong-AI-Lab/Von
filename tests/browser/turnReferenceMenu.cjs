// Local browser fixture: actual chat renderer, menu, clipboard and reference client.
// Server responses are fixtures; backend resolution is covered separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const reference = '#V#conversation_fixture_turn_612d73746f726564';
const calls = [];
let unavailable = false;
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        res.end('<!doctype html><link rel="stylesheet" href="/static/styles.css"><div id="scrollableField"></div>');
        return;
    }
    if (req.url === '/von/api/session/conversation_reference') {
        let body = '';
        req.on('data', data => body += data);
        req.on('end', () => {
            calls.push(JSON.parse(body));
            res.setHeader('Content-Type', 'application/json');
            res.end(JSON.stringify(unavailable ? { success: false } : { success: true, concept_reference: reference }));
        });
        return;
    }
    const file = path.resolve(root, `.${req.url.split('?')[0]}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) {
        res.setHeader('Content-Type', 'application/json');
        res.end('{}');
        return;
    }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage({ permissions: ['clipboard-read', 'clipboard-write'] });
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        async function render() {
            await page.evaluate(async () => {
                const chat = await import('/static/js/chatTab.js');
                chat.__testOnly_setActiveChatSession('source-session', 'Source');
                chat.__testOnly_appendMessage('User', 'Saved question', 'u-stored', false, true);
                chat.__testOnly_appendMessage('Von', 'Saved answer', 'a-stored', false, true);
            });
        }
        await render();
        const turn = page.locator('.message-container[data-turn-id="a-stored"]');
        await turn.locator('.chat-message-text').click({ button: 'right' });
        await expect(page.getByRole('menuitem', { name: 'Copy turn reference' })).toBeFocused();
        await page.getByRole('menuitem', { name: 'Copy turn reference' }).click();
        await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(reference);
        assert.deepEqual(calls.at(-1), { session_id: 'source-session', metadata_only: true, turn_id: 'a-stored' });
        await expect(turn).toBeFocused();
        await page.reload();
        await render();
        await turn.focus();
        await page.keyboard.press('Shift+F10');
        await expect(page.getByRole('menuitem', { name: 'Copy turn reference' })).toBeFocused();
        const evidence = process.argv[2];
        if (evidence) {
            fs.mkdirSync(evidence, { recursive: true });
            await page.screenshot({ path: path.join(evidence, 'turn-reference-menu.png') });
        }
        await page.keyboard.press('Enter');
        await expect.poll(() => calls.length).toBe(2);
        await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(reference);
        await turn.focus();
        await page.keyboard.press('Shift+F10');
        await page.keyboard.press('Escape');
        await expect(turn).toBeFocused();
        unavailable = true;
        await page.evaluate(() => navigator.clipboard.writeText('preserved'));
        await turn.locator('.conversation-turn-copy').click();
        await expect(page.getByText('This entry has no available stored turn reference.', { exact: true })).toBeVisible();
        assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'preserved');
        unavailable = false;
        await page.evaluate(() => {
            navigator.clipboard.writeText = async () => { throw new Error('Clipboard denied'); };
            document.execCommand = () => false;
        });
        await turn.locator('.conversation-turn-copy').click();
        await expect(page.getByText('Could not copy turn reference.', { exact: true })).toBeVisible();
        console.log('PASS: right-click, keyboard/focus, clipboard, reload stability, unavailable stored turn and clipboard failure');
    } finally {
        await browser.close();
        server.close();
    }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
