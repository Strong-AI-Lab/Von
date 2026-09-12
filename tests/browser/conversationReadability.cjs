// Explicitly isolated fixture acceptance: real templates/modules, in-memory API state.
// No authentication, live messages, database writes, or model calls are exercised.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/conversationReadability.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '/tmp/conversation-readability';
fs.mkdirSync(evidence, { recursive: true });
function template(name) {
    return fs.readFileSync(path.join(root, 'templates', name), 'utf8')
        .replace(/{% include '([^']+)' %}/g, (_, child) => template(child))
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
        .replace(/{{ url_for\('static', filename='([^']+)'\) }}/g, '/static/$1')
        .replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
}
const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(template('von_interface.html')); }
    const file = path.resolve(root, `.${pathname}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : file.endsWith('.png') ? 'image/png' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
const actor = '#V#alice', other = '#V#bob';
const row = { session_id: 'messages:fixture', source_kind: 'message_exchange', viewer_id: actor,
    participant_ids: [actor, other], other_participant_ids: [other], session_name: 'Bob fixture', organisation_concept_id: null,
    last_message_at: '2026-09-12T10:59:00Z', preview: 'Fixture conversation' };
let messages, reads, failRead;
function seed() {
    reads = []; failRead = false;
    messages = Array.from({ length: 60 }, (_, i) => ({ concept_id: `fixture-${i}`, created_at: `2026-09-12T10:${String(i).padStart(2, '0')}:00Z`,
        relationships: { '#V#has_sender': [i % 2 ? actor : other], '#V#has_recipient': [i % 2 ? other : actor] },
        concept_data: { read_by: [], content_fallback: `Contribution ${i}. ` + 'A research update with observations, provenance and next steps. '.repeat(5) } }));
}
const unread = () => messages.filter(m => m.relationships['#V#has_recipient'].includes(actor) && !m.concept_data.read_by.includes(actor));
async function setup(page) {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async ({ row, actor }) => {
        document.body.classList.remove('von-auth-pending');
        document.querySelector('#vonAuthenticationGate').hidden = true;
        document.querySelector('#vonAuthenticatedApp').hidden = false;
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: actor }));
        const workspace = document.querySelector('#conversationWorkspace');
        const { createConversationTray } = await import('/static/js/components/conversationTray.js');
        const tray = createConversationTray(workspace);
        const refresh = () => { workspace.dataset.effectiveTabsLayout = innerWidth <= 900 ? 'horizontal' : 'vertical'; tray.refresh({ width: 260, collapsed: false, expandOnHover: true }); };
        refresh(); window.addEventListener('resize', refresh);
        const { setupDynamicLayout } = await import('/static/js/components/dynamicLayout.js'); setupDynamicLayout();
        const catalogue = await import('/static/js/components/conversationCatalogue.js');
        window.fixtureCatalogue = catalogue;
        window.fixturePanel = await import('/static/js/components/messagePanel.js');
        const render = () => {
            const tabs = document.querySelector('#chatSessionTabs'); tabs.replaceChildren();
            catalogue.directConversationRows().forEach(item => tabs.append(catalogue.renderMessageConversationRow(item)));
        };
        catalogue.initialiseConversationCatalogue({ render });
        await catalogue.refreshMessageCatalogue();
        await catalogue.selectMessageConversation(row);
    }, { row, actor });
}
async function measure(page, name) {
    const result = await page.evaluate(() => {
        const root = document.querySelector('#messageViewContent');
        const box = el => { const r = el.getBoundingClientRect(); return { x: r.x, right: r.right, width: r.width }; };
        return { width: innerWidth, overflow: document.documentElement.scrollWidth - innerWidth,
            paneOverflow: root.scrollWidth - root.clientWidth,
            sent: box(document.querySelector('.message-bubble.sent')), received: box(document.querySelector('.message-bubble.received')),
            badge: getComputedStyle(document.querySelector('.chat-session-tab-unread')).backgroundColor };
    });
    assert(result.sent.x > result.received.x + 5, `${name}: own messages on right`);
    assert(result.overflow <= 1 && result.paneOverflow <= 1, `${name}: overflow ${JSON.stringify(result)}`);
    assert.equal(result.badge, 'rgb(37, 99, 235)');
    await page.screenshot({ path: path.join(evidence, `${name}.png`) });
    return { name, ...result };
}
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    const results = [];
    try {
        for (const width of [1440, 360]) {
            seed();
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            await page.route('**/*', async route => {
                const req = route.request(), url = new URL(req.url());
                if (url.pathname.startsWith('/static/') || url.pathname === '/') return route.continue();
                if (url.pathname === '/api/messages/exchange') {
                    const before = req.postDataJSON().before;
                    const eligible = messages.filter(m => !before || m.created_at < before.created_at);
                    const rows = eligible.slice(-50);
                    return route.fulfill({ json: { current_user_id: actor, messages: rows, before: eligible.length > 50 ? { created_at: rows[0].created_at, concept_id: rows[0].concept_id } : null } });
                }
                if (url.pathname === '/api/messages/read/bulk') {
                    const ids = req.postDataJSON().message_ids;
                    if (failRead) return route.fulfill({ status: 503, json: { error: 'isolated failure' } });
                    reads.push(ids);
                    for (const id of ids) messages.find(m => m.concept_id === id).concept_data.read_by.push(actor);
                    return route.fulfill({ json: { success: true, updated_count: ids.length } });
                }
                if (url.pathname.includes('conversation-catalogue')) return route.fulfill({ json: { conversations: [{ ...row, shared_unread_count: unread().length }] } });
                if (url.pathname.endsWith('/participants/profiles')) return route.fulfill({ json: { profiles: [actor, other].map(concept_id => ({ concept_id, display_name: concept_id === actor ? 'Alice' : 'Bob' })) } });
                // All remaining non-static calls are fixtures. Never forward to a live API.
                return route.fulfill({ json: {} });
            });
            await setup(page);
            await expect.poll(() => reads.length).toBeGreaterThan(0);
            assert(unread().length > 20, 'Opening cannot clear unseen messages');
            assert(unread().some(m => m.concept_id === 'fixture-0'), 'unloaded page stays unread');
            await expect(page.locator('.chat-session-tab-unread')).toHaveText(String(unread().length));
            results.push(await measure(page, `messages-${width}`));
            const firstUnread = page.locator('.message-bubble.is-unread').first();
            const id = await firstUnread.getAttribute('data-contribution-id');
            failRead = true;
            await firstUnread.scrollIntoViewIfNeeded();
            await expect(page.locator('.message-read-status')).toBeVisible();
            assert(unread().some(m => m.concept_id === id));
            failRead = false;
            await page.locator('.message-read-status button').click();
            await expect.poll(() => unread().some(m => m.concept_id === id)).toBe(false);
            await page.evaluate(() => fixturePanel.refreshOpenMessageExchange());
            await expect(page.locator(`[data-contribution-id="${id}"] .message-unread-label`)).toHaveCount(0);
            // An arrival stays unread while the reader is inspecting earlier content.
            messages.push({ concept_id: 'arrival', created_at: '2026-09-12T11:01:00Z', relationships: { '#V#has_sender': [other], '#V#has_recipient': [actor] }, concept_data: { read_by: [], content_fallback: 'A new incoming research update.' } });
            await page.evaluate(() => fixturePanel.refreshOpenMessageExchange());
            await expect(page.locator('.message-jump-latest')).toBeVisible();
            assert(unread().some(m => m.concept_id === 'arrival'));
            await page.locator('.message-jump-latest').click();
            await expect.poll(() => unread().some(m => m.concept_id === 'arrival')).toBe(false);
            await page.getByRole('button', { name: 'Load earlier messages', exact: true }).click();
            await expect(page.locator('[data-contribution-id="fixture-0"]')).toHaveCount(1);
            assert(unread().some(m => m.concept_id === 'fixture-0'));
            await page.locator('[data-contribution-id="fixture-0"]').scrollIntoViewIfNeeded();
            await expect.poll(() => unread().some(m => m.concept_id === 'fixture-0')).toBe(false);
            const persisted = unread().length;
            await setup(page); // Simulate page reload against retained canonical fixture state.
            assert(unread().length <= persisted);
            assert(!unread().some(m => m.concept_id === id));
            await page.evaluate(async () => {
                fixtureCatalogue.showChatConversation();
                const chat = await import('/static/js/chatTab.js');
                document.querySelector('#scrollableField').replaceChildren();
                chat.__testOnly_appendMessage('Alice', 'An own contribution with the existing edit and copy controls.', 'own', false, true, null, null, null, { authorConceptId: '#V#alice' });
                chat.__testOnly_appendMessage('Bob', 'A contribution from another participant, preserving its sender label.', 'other', false, true, null, null, null, { authorConceptId: '#V#bob' });
                chat.__testOnly_appendMessage('Von', 'A useful assistant response with retained controls and readable content.', 'assistant', false, true);
            });
            const layout = await page.evaluate(() => [...document.querySelectorAll('#scrollableField > .message-container')].map(el => ({ classes: el.className, x: el.getBoundingClientRect().x, right: el.getBoundingClientRect().right, overflow: el.scrollWidth - el.clientWidth, text: el.textContent })));
            assert(layout[0].x > layout[1].x + 5 && layout[0].x > layout[2].x + 5, 'Old-style participant alignment');
            assert(layout.every(item => item.overflow <= 1), 'Old-style controls/content fit');
            assert(layout[1].text.includes('Bob'));
            results.push({ name: `chat-${width}`, layout });
            await page.screenshot({ path: path.join(evidence, `chat-${width}.png`) });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify({ fixture: true, results }, null, 2));
        console.log(JSON.stringify({ passed: true, evidence, cases: results.length }));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
