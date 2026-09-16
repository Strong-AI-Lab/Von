// Production message pane and CSS with synthetic paginated HTTP responses.
// No live authentication, messages or read-state writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8').replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
const evidence = process.argv[2] || '.run/unread-navigation';
fs.mkdirSync(evidence, { recursive: true });
let cursors = [];
let sent = [];
let background = false;
let pendingPage = null;
let zeroTransition = false;
let pendingRead = null;
const readMessages = new Set();
const message = (id, unread, day) => ({ concept_id: id, created_at: `2026-09-${day}T12:00:00Z`,
    relationships: { '#V#has_sender': ['#V#bob'], '#V#has_recipient': ['#V#alice'] },
    concept_data: { content_fallback: `${id}: ${'A realistic message for checking the reading position. '.repeat(5)}`, read_by: unread ? [] : ['#V#alice'] } });
const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end(`<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css">${markup}`);
    }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
            return res.end(fs.readFileSync(file));
        }
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/messages/' && req.method === 'POST') {
        let body = ''; for await (const chunk of req) body += chunk;
        sent.push(JSON.parse(body));
        return res.end(JSON.stringify({ success: true, message_id: '#V#fixture_sent' }));
    }
    if (req.url === '/api/messages/read/bulk' && zeroTransition) {
        let body = ''; for await (const chunk of req) body += chunk;
        const { message_ids } = JSON.parse(body);
        pendingRead = () => {
            message_ids.forEach(id => readMessages.add(id));
            res.end(JSON.stringify({ success: true, updated_count: message_ids.length }));
        };
        return;
    }
    if (req.url === '/api/messages/exchange') {
        let body = ''; for await (const chunk of req) body += chunk;
        const { before, participant_ids } = JSON.parse(body); cursors.push(before);
        if (zeroTransition) {
            const id = participant_ids.includes('#V#carol') ? 'carol-final' : 'bob-final';
            return res.end(JSON.stringify({ current_user_id: '#V#alice', before: 'read-history',
                messages: [message(id, !readMessages.has(id), '15')] }));
        }
        const initial = Array.from({ length: 20 }, (_, index) => message(`latest-${String(index).padStart(2, '0')}`, false, '13'));
        const result = !before ? { messages: background ? [...initial, message('Background unread', true, '14')] : initial, before: 'middle' }
            : before === 'middle' ? { messages: Array.from({ length: 10 }, (_, index) => message(`middle-${index}`, index === 5, '12')), before: 'older' }
                : { messages: [message('Earlier unread', true, '10'), message('Most recent unread', true, '11')], before: null };
        const finish = () => res.end(JSON.stringify({ ...result, current_user_id: '#V#alice' }));
        if (before || background) pendingPage = finish;
        else finish();
        return;
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
            sent = [];
            background = false;
            zeroTransition = false;
            const page = await browser.newPage({ viewport: { width, height: 850 } });
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                // Keep read markers stable while checking navigation independently.
                window.IntersectionObserver = undefined;
                document.getElementById('conversationWorkspace').classList.add('show-message-exchange');
                document.body.classList.add('viewing-message-exchange');
                const { openMessageExchange } = await import('/static/js/components/messagePanel.js');
                await openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice',
                    participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'],
                    session_name: 'Unread pagination fixture', shared_unread_count: 3 });
            });
            const content = page.locator('#messageViewContent');
            assert.equal(await content.evaluate(el => el.scrollTop), 0, 'default loading preserves the top');
            const send = page.getByRole('button', { name: 'Send message', exact: true });
            const input = page.locator('#messageInput');
            await input.fill('A reply from the up-arrow control.');
            await expect(send).toBeVisible();
            await expect(send.locator('svg path')).toHaveAttribute('d', await page.locator('#sendButton svg path').getAttribute('d'));
            assert.equal(await send.evaluate(el => getComputedStyle(el).borderRadius), '50%');
            const latest = page.getByRole('button', { name: 'Scroll to latest message', exact: true });
            await expect(latest).toHaveText('Latest message');
            const box = await send.boundingBox();
            assert.ok(box.width >= 44 && box.height >= 44, 'send target is at least 44px');
            assert.ok(box.x >= 0 && box.x + box.width <= width, 'send stays in viewport');
            await page.screenshot({ path: path.join(evidence, `composer-${width}.png`) });
            await latest.click();
            await expect(latest).toBeHidden();
            assert.equal(sent.length, 0, 'latest navigation does not send the draft');
            await expect(input).toHaveValue('A reply from the up-arrow control.');
            await content.evaluate(el => { el.scrollTop = el.scrollHeight - el.clientHeight - 10; });
            const bottomPosition = await content.evaluate(el => el.scrollTop);
            assert.ok(bottomPosition > 0, 'fixture must overflow');
            background = true;
            await page.evaluate(() => {
                window.refreshDone = import('/static/js/components/messagePanel.js').then(panel => panel.refreshOpenMessageExchange());
            });
            await expect.poll(() => Boolean(pendingPage)).toBe(true);
            assert.equal(await content.evaluate(el => el.scrollTop), bottomPosition, 'pending background request must preserve position');
            pendingPage(); pendingPage = null;
            await page.evaluate(() => window.refreshDone);
            assert.equal(await content.evaluate(el => el.scrollTop), bottomPosition, 'background unread must not follow the bottom');
            // Reopen the initial fixture, then traverse delayed earlier pages.
            background = false;
            await page.evaluate(async () => {
                const panel = await import('/static/js/components/messagePanel.js');
                await panel.openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice',
                    participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'],
                    session_name: 'Unread pagination fixture', shared_unread_count: 3 });
            });
            cursors = [];
            await content.evaluate(el => { el.scrollTop = 450; });
            const anchor = await content.evaluate(el => {
                const top = el.getBoundingClientRect().top;
                const item = [...el.querySelectorAll('[data-contribution-id]')].find(item => item.getBoundingClientRect().bottom > top);
                return { id: item.dataset.contributionId, offset: item.getBoundingClientRect().top - top };
            });
            const offset = () => page.locator(`[data-contribution-id="${anchor.id}"]`).evaluate(el =>
                el.getBoundingClientRect().top - document.getElementById('messageViewContent').getBoundingClientRect().top);
            await page.getByRole('button', { name: 'Jump to first unread', exact: true }).click();
            await expect.poll(() => Boolean(pendingPage)).toBe(true);
            assert.equal(await offset(), anchor.offset, 'pending go-to-unread preserves the visible message');
            pendingPage(); pendingPage = null;
            await expect.poll(() => Boolean(pendingPage)).toBe(true);
            await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
            assert.ok(Math.abs(await offset() - anchor.offset) <= 1, 'intermediate page preserves the visible message within one CSS pixel');
            await expect(page.getByRole('button', { name: 'Loading unread messages…', exact: true })).toBeVisible();
            await page.screenshot({ path: path.join(evidence, `loading-${width}.png`) });
            pendingPage(); pendingPage = null;
            const target = page.locator('[data-contribution-id="Earlier unread"]');
            await expect(target).toBeFocused();
            await expect(target).toBeInViewport();
            assert.deepEqual(cursors, ['middle', 'older']);
            await page.screenshot({ path: path.join(evidence, `unread-${width}.png`) });
            await send.click();
            await expect(input).toHaveValue('');
            assert.equal(sent.length, 1);
            assert.equal(sent[0].content, 'A reply from the up-arrow control.');
            assert.deepEqual(errors, []);
            results.push({ width, cursors: [...cursors], sendArrow: true, sendTarget: box, sends: sent.length, labelledLatestNavigation: true, defaultTopPreserved: true, backgroundPosition: bottomPosition, intermediateAnchorOffset: anchor.offset, firstUnreadFocused: true, targetInViewport: true });
            await page.close();

            // Real IntersectionObserver and confirmed HTTP receipt: the last
            // unread disappears while an older-history cursor still exists.
            zeroTransition = true;
            readMessages.clear();
            const reading = await browser.newPage({ viewport: { width, height: 850 } });
            await reading.goto(`http://127.0.0.1:${server.address().port}`);
            const open = (other, count) => reading.evaluate(async ({ other, count }) => {
                document.getElementById('conversationWorkspace').classList.add('show-message-exchange');
                document.body.classList.add('viewing-message-exchange');
                const panel = await import('/static/js/components/messagePanel.js');
                await panel.openMessageExchange({ session_id: `messages:${other}`, viewer_id: '#V#alice',
                    participant_ids: ['#V#alice', other], other_participant_ids: [other],
                    session_name: other, shared_unread_count: count });
            }, { other, count });
            await open('#V#bob', 1);
            const unreadButton = reading.locator('.message-jump-unread');
            await expect(unreadButton).toBeVisible();
            await expect.poll(() => Boolean(pendingRead)).toBe(true);
            pendingRead(); pendingRead = null;
            await expect(reading.locator('.is-unread')).toHaveCount(0);
            await expect(unreadButton).toBeHidden();
            await expect(reading.getByRole('button', { name: 'Load earlier messages', exact: true })).toBeVisible();
            await reading.screenshot({ path: path.join(evidence, `zero-unread-${width}.png`) });
            await open('#V#carol', 1);
            await expect(unreadButton).toBeVisible();
            await expect.poll(() => Boolean(pendingRead)).toBe(true);
            await open('#V#bob', 0);
            pendingRead(); pendingRead = null;
            await expect(unreadButton).toBeHidden();
            results[results.length - 1].zeroUnreadHiddenWithOlderHistory = true;
            results[results.length - 1].switchAndReopenCorrect = true;
            await reading.close();
        }
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
