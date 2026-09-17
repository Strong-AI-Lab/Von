// Queue completion acceptance using production templates and queue/send/switch code.
// No authentication, live messages, database writes, or model calls are exercised.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/conversationQueueCompletion.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/queue-evidence/browser';
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

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    const results = [];
    try {
        for (const width of [1440, 390]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            const finished = { queue_id: 'finished', session_id: 'first', session_name: 'Find Email from Yitan', prompt_raw: 'Find Email from Yitan', status: 'queued' };
            const pending = { queue_id: 'pending', session_id: 'first', prompt_raw: 'A distinct unsent request', status: 'queued' };
            let terminal = false;
            let finishGenerate;
            let releaseOld;
            let holdRead = false;
            const calls = [];
            await page.route('**/*', async route => {
                const url = new URL(route.request().url());
                if (!url.pathname.includes('/api/') && !url.pathname.startsWith('/von/')) return route.continue();
                calls.push({ path: url.pathname, method: route.request().method() });
                let data = {};
                if (url.pathname === '/von/api/chat_prompt_queue') {
                    if (route.request().method() === 'POST') data = { item: finished };
                    else {
                        data = { items: terminal ? [pending] : [finished, pending] };
                        if (holdRead) {
                            holdRead = false;
                            await new Promise(resolve => { releaseOld = resolve; });
                        }
                    }
                } else if (url.pathname === '/von/generate') {
                    await new Promise(resolve => { finishGenerate = resolve; });
                    terminal = true;
                    data = { response: 'Completed fixture response.' };
                } else if (url.pathname === '/von/api/session/set_chat_session') data = { session_id: route.request().postDataJSON().session_id };
                else if (url.pathname === '/von/history') data = { history: [], session_id: url.searchParams.get('session_id'), authenticated: true };
                else if (url.pathname === '/von/history/length') data = { history_length: 0, authenticated: true };
                else if (url.pathname === '/von/api/render_markdown') data = { html: route.request().postDataJSON().text || '' };
                await route.fulfill({ json: data });
            });
            const mount = () => page.evaluate(async () => {
                document.body.classList.remove('von-auth-pending');
                document.querySelector('#vonAuthenticationGate').hidden = true;
                document.querySelector('#vonAuthenticatedApp').hidden = false;
                localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#fixture_user' }));
                window.chat = await import('/static/js/chatTab.js');
                chat.__testOnly_setActiveChatSession('first', 'Find Email from Yitan');
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await mount();
            await page.locator('#promptInput').fill(finished.prompt_raw);
            await page.evaluate(() => { window.sending = chat.sendMessage(); });
            await expect.poll(() => Boolean(finishGenerate)).toBe(true);
            await page.evaluate(() => chat.switchToChatSession('second'));
            holdRead = true;
            await page.evaluate(() => { window.oldRead = chat.__testOnly_refreshChatPromptQueueFromServer(); });
            await expect.poll(() => Boolean(releaseOld)).toBe(true);
            finishGenerate();
            await page.evaluate(() => sending);
            await expect.poll(() => calls.filter(c => c.path === '/von/api/chat_prompt_queue' && c.method === 'GET').length).toBe(2);
            // Wait for the completion read to apply before releasing the stale response.
            await page.evaluate(() => chat.__testOnly_setActiveChatSession('first', 'Find Email from Yitan'));
            await expect(page.locator('.chat-task-queue-edit')).toHaveValue(pending.prompt_raw);
            const historyReads = () => calls.filter(c => c.path === '/von/history').length;
            const countBeforeOld = historyReads();
            releaseOld();
            assert.equal(await page.evaluate(() => oldRead), false);
            assert.equal(historyReads(), countBeforeOld);
            await page.evaluate(() => chat.switchToChatSession('first'));
            await expect(page.locator('.chat-task-queue-edit')).toHaveCount(1);
            await expect(page.locator('.chat-task-queue-edit')).toHaveValue(pending.prompt_raw);
            await expect(page.locator('#chatTaskQueueCount')).toContainText('1');
            await page.screenshot({ path: path.join(evidence, `${width}-pending-only.png`) });
            await page.reload();
            await mount();
            await page.evaluate(() => chat.__testOnly_refreshChatPromptQueueFromServer());
            await expect(page.locator('.chat-task-queue-edit')).toHaveCount(1);
            await expect(page.locator('.chat-task-queue-edit')).toHaveValue(pending.prompt_raw);
            assert.equal(calls.filter(c => c.path === '/von/generate').length, 1);
            assert.equal(calls.some(c => /\/(finish|claim)$/.test(c.path) || c.method === 'DELETE'), false);
            results.push({ width, stale_read_ignored: true, switched_and_remounted: true, unrelated_queue_preserved: true, executions: 1 });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify({ mode: 'production modules with local HTTP fixtures; no live authentication or model calls', results }, null, 2));
        console.log(JSON.stringify(results));
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
