// Composer state acceptance using production templates, CSS and send/switch code.
// No authentication, live messages, database writes, or model calls are exercised.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/composerDraftLifecycle.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/composer-evidence/browser';
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
            let finishGenerate;
            let rejectNext = false;
            const sent = [];
            await page.route('**/*', async route => {
                const url = new URL(route.request().url());
                if (!url.pathname.includes('/api/') && !['/von/generate', '/von/history', '/von/history/length', '/von/progress'].includes(url.pathname)) return route.continue();
                let data = {};
                if (url.pathname === '/von/generate') {
                    sent.push(route.request().postDataJSON());
                    if (rejectNext) {
                        rejectNext = false;
                        return route.fulfill({ status: 400, json: { error: 'Fixture rejection' } });
                    }
                    await new Promise(resolve => { finishGenerate = resolve; });
                    finishGenerate = null;
                    data = { response: 'Fixture received the caption.', llm_debug: { model: 'fixture-no-model-call' } };
                } else if (url.pathname === '/von/api/images/upload') data = { image_attachment: { concept_id: '#V#fixture_image', filename: 'attachment.png' } };
                else if (url.pathname === '/von/api/session/set_chat_session') data = { session_id: route.request().postDataJSON().session_id };
                else if (url.pathname === '/von/history') data = { history: [], session_id: url.searchParams.get('session_id'), authenticated: true };
                else if (url.pathname === '/von/history/length') data = { history_length: 0, authenticated: true };
                else if (url.pathname === '/von/api/render_markdown') data = { html: route.request().postDataJSON().text || '' };
                await route.fulfill({ json: data });
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                document.body.classList.remove('von-auth-pending');
                document.querySelector('#vonAuthenticationGate').hidden = true;
                document.querySelector('#vonAuthenticatedApp').hidden = false;
                localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#fixture_user' }));
                window.chat = await import('/static/js/chatTab.js');
                const { initialiseConversationDraft } = await import('/static/js/components/conversationDraft.js');
                chat.__testOnly_setActiveChatSession('first', 'First');
                initialiseConversationDraft({ input: document.querySelector('#promptInput'), sendButton: document.querySelector('#sendButton'), onSubmit: () => { window.pendingSend = chat.sendMessage(); } });
            });
            const input = page.locator('#promptInput');
            const select = session => page.evaluate(session => chat.switchToChatSession(session), session);
            const upload = () => page.evaluate(() => chat.__testOnly_uploadFilesToVon([new File(['fixture'], 'attachment.png', { type: 'image/png' })]));
            const caption = process.env.VON_COMPOSER_REPRO_FILE
                ? fs.readFileSync(process.env.VON_COMPOSER_REPRO_FILE, 'utf8')
                : 'Submitted multiline caption.\nPlease inspect the attached footer.';
            await input.fill('First unsent draft');
            await select('second');
            await expect(input).toHaveValue('');
            await input.fill('Second unsent draft');
            await select('first');
            await expect(input).toHaveValue('First unsent draft');
            await upload();
            await input.fill(caption);
            await page.locator('#sendButton').click();
            await expect.poll(() => Boolean(finishGenerate)).toBe(true);
            await select('second');
            await expect(input).toHaveValue('Second unsent draft');
            finishGenerate();
            await page.evaluate(() => pendingSend);
            await expect(input).toHaveValue('Second unsent draft');
            await select('first');
            await expect(input).toHaveValue('');
            await upload();
            await input.fill('Retry caption');
            rejectNext = true;
            await page.locator('#sendButton').click();
            await page.evaluate(() => pendingSend);
            await expect(input).toHaveValue('Retry caption');
            await select('second');
            await expect(input).toHaveValue('Second unsent draft');
            await select('first');
            await expect(input).toHaveValue('Retry caption');
            await page.locator('#sendButton').click();
            await expect.poll(() => Boolean(finishGenerate)).toBe(true);
            finishGenerate();
            await page.evaluate(() => pendingSend);
            await expect(input).toHaveValue('');
            assert.equal(sent.length, 3);
            assert.equal(sent[0].prompt, caption);
            assert.deepEqual(sent[1].image_attachment_ids, sent[2].image_attachment_ids);
            await page.screenshot({ path: path.join(evidence, `${width}-cleared.png`) });
            await page.reload();
            await expect(input).toHaveValue('');
            results.push({ width, background_send_cleared: true, draft_isolation: true, failure_retry: true, reload_empty: true, requests: sent.length });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify({ mode: 'production modules with local HTTP fixtures; no live authentication or model calls', results }, null, 2));
        console.log(JSON.stringify(results));
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
