// Production UI modules and stylesheet; isolated synthetic HTTP/clipboard fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/message-menu';
fs.mkdirSync(evidence, { recursive: true });
let references = 0;
const server = http.createServer((req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); return res.end('<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><div id="chatSessionTabs"></div>'); }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript'); return res.end(fs.readFileSync(file));
        }
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url.includes('/exchange/reference')) { references++; return res.end(JSON.stringify({ success: true, concept_id: '#V#message_exchange_reference_fixture' })); }
    res.end(JSON.stringify({ success: true, authenticated: true, messages: [], profiles: [] }));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [320, 448, 1280]) {
            const page = await browser.newPage({ viewport: { width, height: 891 }, hasTouch: true });
            const errors = []; page.on('pageerror', e => errors.push(e.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                window.copied = [];
                Object.defineProperty(navigator, 'clipboard', { value: { writeText: async text => window.copied.push(text) } });
                const { renderMessageConversationRow } = await import('/static/js/components/conversationCatalogue.js');
                const { openChatSessionMenu } = await import('/static/js/chatTab.js');
                const row = { session_id: 'messages:fixture', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#participant_with_a_deliberately_long_concept_identifier_for_mobile_wrapping'], other_participant_ids: ['#V#bob'], session_name: 'Coding agent conversation', last_message_at: '2026-09-13', organisation_concept_id: '#V#lab' };
                document.querySelector('#chatSessionTabs').append(renderMessageConversationRow(row, { openMenu: openChatSessionMenu, togglePin: () => {}, hide: () => {} }));
            });
            const trigger = page.getByRole('button', { name: 'Conversation actions for Coding agent conversation' });
            await trigger.tap();
            const menu = page.getByRole('menu');
            await expect(menu).toBeVisible();
            const geometry = await menu.evaluate(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width, overflow: el.scrollWidth - el.clientWidth }; });
            assert(geometry.left >= 0 && geometry.right <= width && geometry.overflow <= 1, JSON.stringify(geometry));
            await page.screenshot({ path: path.join(evidence, `menu-${width}.png`) });
            await page.getByRole('menuitem', { name: 'Copy Concept ID', exact: true }).click();
            await expect.poll(() => page.evaluate(() => window.copied[0])).toBe('#V#message_exchange_reference_fixture');
            await trigger.focus(); await page.keyboard.press('Shift+F10');
            await expect(menu).toBeVisible();
            await page.keyboard.press('Escape');
            await expect(menu).toBeHidden(); await expect(trigger).toBeFocused();
            await trigger.tap();
            await page.getByRole('menuitem', { name: 'Copy conversation reference', exact: true }).click();
            const reference = JSON.parse(await page.evaluate(() => window.copied[1]));
            assert.equal(reference.message_lookup.organisation_concept_id, '#V#lab');
            assert.equal(reference.message_lookup.participant_ids.length, 2);
            assert.deepEqual(errors, []);
            results.push({ width, geometry, copyConceptId: true, copyReference: true, keyboardEscapeFocus: true });
            await page.close();
        }
        assert.equal(references, 3);
        fs.writeFileSync(path.join(evidence, 'browser-result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
