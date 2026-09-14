// Production template/CSS and interaction modules with deterministic steering transport.
// This fixture performs no authentication, model calls or persisted Von mutations.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/conversationActions.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const output = process.argv[2] || '.run/conversation-ui';
fs.mkdirSync(output, { recursive: true });
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const server = http.createServer((req, res) => {
    if (req.url === '/') return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css">${markup}`);
    if (req.url.includes('/api/')) {
        res.setHeader('Content-Type', 'application/json');
        return res.end(JSON.stringify({ messages: [], current_user_id: '#V#fixture_viewer', unread_count: 0 }));
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    const measurements = [];
    try {
        for (const [width, height] of [[1440,900], [375,812], [667,375]]) {
            const page = await browser.newPage({ viewport: { width, height }, hasTouch: width < 800, isMobile: width < 800 });
            page.setDefaultTimeout(8000);
            await page.goto(`http://127.0.0.1:${server.address().port}/`);
            await page.evaluate(async () => {
                document.querySelector('#messagesContainer').style.display = 'none';
                document.querySelector('#chatSessionMetadata').innerHTML = '<h2 class="chat-session-current-title">A conversation with a long research title</h2><button>Conversation context ▸</button>';
                document.querySelector('#scrollableField').textContent = 'A saved conversation. The active turn is working.';
                const { initialiseConversationActions } = await import('/static/js/components/conversationActions.js');
                initialiseConversationActions();
                const { initialiseCompactChatComposer, shouldSubmitComposerKey } = await import('/static/js/components/compactComposer.js');
                const { createChatSteeringControls } = await import('/static/js/components/chatSteeringControls.js');
                const input = document.querySelector('#promptInput');
                const send = document.querySelector('#sendButton');
                window.posts = []; window.queues = 0; window.items = [];
                window.control = createChatSteeringControls({ sendButton: send,
                    getDraft: () => input.value, clearDraft: text => { if (input.value === text) input.value = ''; },
                    createId: () => `id-${window.posts.length}`, onQueue: () => window.queues++,
                    onModeChange: () => window.control.present(),
                    request: async (url, options) => {
                        if (options.method === 'POST') { const body = JSON.parse(options.body); window.posts.push({ url, ...body }); window.items.push({ id: body.submission_id, text: body.text, status: 'pending' }); }
                        if (options.method === 'DELETE') window.items.forEach(item => item.status = 'cancelled');
                        return { items: window.items };
                    }
                });
                window.state = { scopeKey: 'fixture:session', actorKey: 'fixture', busy: true, target: { queueId: 'queue', attemptId: 'attempt' }, disabled: false };
                window.control.update(window.state); window.control.present();
                const submit = event => { if (!window.control.activate(event)) window.queues++; };
                send.addEventListener('click', submit);
                input.addEventListener('keypress', event => { if (shouldSubmitComposerKey(event)) { event.preventDefault(); submit(event); } });
                input.addEventListener('input', () => { send.dataset.hasDraft = String(Boolean(input.value.trim())); });
                initialiseCompactChatComposer(document.querySelector('.chat-composer'));
            });
            const header = page.locator('#conversationActions');
            await header.locator('summary').click();
            await expect(page.getByRole('button', { name: 'Invite people', exact: true })).toBeVisible();
            await page.getByRole('button', { name: 'Invite people', exact: true }).focus();
            await page.keyboard.press('Escape');
            await expect(header.locator('summary')).toBeFocused();
            await expect(header).not.toHaveAttribute('open', '');
            const input = page.locator('#promptInput');
            await input.fill('A long correction '.repeat(40));
            const before = await input.boundingBox();
            await page.locator('#sendButton').click({ modifiers: ['Shift'] });
            await expect(input).toHaveValue('');
            assert.equal(await page.evaluate(() => posts[0].attempt_id), 'attempt');
            await input.fill('Second long correction '.repeat(40));
            await page.locator('#sendButton').click({ modifiers: ['Shift'] });
            await input.fill('Draft stays useful');
            const after = await input.boundingBox();
            assert(Math.abs(before.width - after.width) < 1, 'Long receipts cannot consume draft width');
            const toast = await page.locator('.chat-steering-toast').boundingBox();
            assert(toast && toast.y >= 0 && toast.y + toast.height <= after.y, 'Notice remains above the composer');
            const more = page.locator('.chat-composer-more-actions > summary');
            await more.click();
            await page.locator('#chatSubmitMode').selectOption('steer');
            await expect(page.locator('#sendButton')).toHaveAccessibleName('Guide active turn');
            await page.locator('.chat-steering-activity > summary').click();
            await expect(page.getByRole('button', { name: 'Cancel steer', exact: true }).first()).toBeVisible();
            await page.getByRole('button', { name: 'Cancel steer', exact: true }).first().click();
            await expect(page.locator('.chat-steering-feedback')).toContainText('Steer cancelled');
            await page.screenshot({ path: path.join(output, `${width}-${height}-activity.png`) });
            await page.locator('#queueDraftButton').click();
            assert.equal(await page.evaluate(() => queues), 1);
            await page.keyboard.press('Escape');
            await input.fill('Default correction');
            await page.locator('#sendButton').click();
            assert.equal(await page.evaluate(() => posts.length), 3);
            await input.fill('New draft');
            await input.press('Shift+Enter');
            assert.equal(await page.evaluate(() => posts.length), 3);
            await page.screenshot({ path: path.join(output, `${width}-${height}.png`) });
            measurements.push({ width, height, before, after, toast, posts: await page.evaluate(() => posts.length) });
            await page.evaluate(async () => {
                window.control.dispose();
                document.querySelector('.chat-conversation-main').style.display = 'none';
                document.querySelector('#messagesContainer').style.display = 'flex';
                document.querySelector('#conversationWorkspace').classList.add('show-message-exchange');
                const { initializeMessagePanel, openMessageExchange } = await import('/static/js/components/messagePanel.js');
                initializeMessagePanel();
                await openMessageExchange({ session_name: 'Research colleague', viewer_id: '#V#fixture_viewer',
                    participant_ids: ['#V#fixture_viewer', '#V#fixture_colleague'], other_participant_ids: ['#V#fixture_colleague'] });
            });
            const exchange = page.locator('#messageViewHeader .conversation-actions');
            await exchange.locator('summary').click();
            await expect(page.getByRole('button', { name: 'Tasks referenced in messages', exact: true })).toBeVisible();
            await expect(exchange.locator('.exchange-profile-button')).toHaveCount(2);
            const menuBox = await exchange.locator('.conversation-actions-panel').boundingBox();
            assert(menuBox.x >= 0 && menuBox.x + menuBox.width <= width + 1 && menuBox.y + menuBox.height <= height + 1, 'Exchange actions fit viewport');
            await exchange.locator('summary').press('Escape');
            await expect(exchange.locator('summary')).toBeFocused();
            const headerBox = await page.locator('#messageViewHeader').boundingBox();
            assert(headerBox.height <= 90, 'Exchange header remains compact');
            await page.screenshot({ path: path.join(output, `${width}-${height}-exchange.png`) });
            await page.close();
        }
        fs.writeFileSync(path.join(output, 'measurements.json'), JSON.stringify(measurements, null, 2));
        console.log('Desktop, phone and landscape disclosure/steering fixture passed.');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
