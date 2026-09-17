// Production UI modules and stylesheet; isolated synthetic HTTP/clipboard fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/message-menu';
fs.mkdirSync(evidence, { recursive: true });
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8').replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
let references = 0;
const server = http.createServer((req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css">${markup}`); }
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
            const page = await browser.newPage({ viewport: { width, height: 891 }, hasTouch: width < 800 });
            page.setDefaultTimeout(10000);
            const errors = []; page.on('pageerror', e => errors.push(e.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                document.querySelector('#messagesContainer').style.display = 'none';
                document.querySelector('#scrollableField').textContent = 'A saved research conversation.';
                localStorage.setItem('von:conversationTrayExpandOnHover', 'false');
                window.copied = [];
                Object.defineProperty(navigator, 'clipboard', { value: { writeText: async text => window.copied.push(text) } });
                const { renderMessageConversationRow, mountCatalogueControls } = await import('/static/js/components/conversationCatalogue.js');
                const { openChatSessionMenu, __testOnly_loadChatSessionTabsLayoutPreference } = await import('/static/js/chatTab.js');
                __testOnly_loadChatSessionTabsLayoutPreference();
                const row = { session_id: 'messages:fixture', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#participant_with_a_deliberately_long_concept_identifier_for_mobile_wrapping'], other_participant_ids: ['#V#bob'], session_name: 'Coding agent conversation', last_message_at: '2026-09-13', organisation_concept_id: '#V#lab' };
                document.querySelector('#chatSessionTabs').append(renderMessageConversationRow(row, { selected: true, openMenu: openChatSessionMenu, togglePin: () => {}, hide: () => {} }));
                mountCatalogueControls(document.querySelector('#chatSessionTabs'));
            });
            const trigger = page.getByRole('tab', { name: /Coding agent conversation/ });
            const touchOptions = page.getByRole('button', { name: 'Selected conversation options', exact: true });
            await expect(page.locator('.conversation-menu-trigger')).toHaveCount(0);
            await expect(trigger).toHaveAttribute('aria-description', /Shift\+F10/);
            assert((await touchOptions.boundingBox()).height >= 44);
            await page.screenshot({ path: path.join(evidence, `rows-${width}.png`) });
            await touchOptions[width < 800 ? 'tap' : 'click']();
            const menu = page.getByRole('menu');
            await expect(menu).toBeVisible();
            const geometry = await menu.evaluate(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width, overflow: el.scrollWidth - el.clientWidth }; });
            assert(geometry.left >= 0 && geometry.right <= width && geometry.overflow <= 1, JSON.stringify(geometry));
            await page.screenshot({ path: path.join(evidence, `menu-${width}.png`) });
            await page.getByRole('menuitem', { name: 'Copy Concept ID', exact: true }).click();
            await expect.poll(() => page.evaluate(() => window.copied[0])).toBe('#V#message_exchange_reference_fixture');
            await trigger.focus(); await page.keyboard.press('Shift+F10');
            await expect(menu).toBeVisible();
            await page.keyboard.press('End');
            await expect(page.getByRole('menuitem').last()).toBeFocused();
            await page.keyboard.press('Home');
            await expect(page.getByRole('menuitem').first()).toBeFocused();
            await page.keyboard.press('ArrowDown');
            await expect(page.getByRole('menuitem', { name: 'Copy Concept ID', exact: true })).toBeFocused();
            await page.keyboard.press('Escape');
            await expect(menu).toBeHidden(); await expect(trigger).toBeFocused();
            await touchOptions[width < 800 ? 'tap' : 'click']();
            await page.getByRole('menuitem', { name: 'Copy conversation reference', exact: true }).click();
            const reference = JSON.parse(await page.evaluate(() => window.copied[1]));
            assert.equal(reference.message_lookup.organisation_concept_id, '#V#lab');
            assert.equal(reference.message_lookup.participant_ids.length, 2);
            for (const gesture of ['right', 'control']) {
                await trigger.click(gesture === 'right' ? { button: 'right' } : { modifiers: ['Control'] });
                await expect(menu).toBeVisible();
                await expect(page.locator('body')).not.toHaveClass(/viewing-message-exchange/);
                await page.keyboard.press('Escape');
                await expect(trigger).toBeFocused();
            }
            await touchOptions[width < 800 ? 'tap' : 'click']();
            await page.keyboard.press('Escape');
            await expect(touchOptions).toBeFocused();
            await page.getByRole('button', { name: 'Collapse conversation list', exact: true }).click();
            await expect(touchOptions).toBeHidden();
            await page.getByRole('button', { name: 'Open conversation list', exact: true }).click();
            await expect(touchOptions).toBeVisible();
            assert.deepEqual(errors, []);
            results.push({ width, geometry, copyConceptId: true, copyReference: true, keyboardEscapeFocus: true, ellipsisAbsent: true, rightClick: true, controlClick: true, touchOptions: true });
            await page.close();
        }
        assert.equal(references, 3);
        fs.writeFileSync(path.join(evidence, 'browser-result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
