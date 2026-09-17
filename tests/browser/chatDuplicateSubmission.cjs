// Real chat send and composer modules, isolated HTTP fixtures; no provider calls.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/chatDuplicateSubmission.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const output = process.argv[2] || '.run/duplicate-send/browser';
fs.mkdirSync(output, { recursive: true });
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8').replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const server = http.createServer((req, res) => {
    if (req.url === '/') return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/styles.css">${markup}`);
    const file = path.resolve(root, `.${req.url.split('?')[0]}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    const evidence = [];
    try {
        for (const width of [1440, 375]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}/`);
            await page.evaluate(async () => {
                window.queueBodies = []; window.generateBodies = [];
                window.fetch = async (url, options = {}) => {
                    const body = JSON.parse(options.body && typeof options.body === 'string' ? options.body : '{}');
                    let data = {};
                    if (url === '/von/api/images/upload') data = { image_attachment: { concept_id: '#V#fixture-image', filename: 'fixture.png' } };
                    else if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                        queueBodies.push(body);
                        data = { success: true, item: { ...body, queue_id: `queue-${queueBodies.length}`, status: 'queued' } };
                    } else if (url === '/von/generate') {
                        generateBodies.push(body);
                        return new Promise(resolve => { window.finishGenerate = () => resolve(new Response(JSON.stringify({ response: 'Received.', llm_debug: { model: 'fixture' } }))); });
                    } else if (String(url).includes('render_markdown')) data = { html: body.text || '' };
                    else if (String(url).includes('history/length')) data = { history_length: 0, authenticated: true };
                    else data = { items: [], history: [] };
                    return new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
                };
                window.chat = await import('/static/js/chatTab.js');
                chat.__testOnly_setActiveChatSession('fixture', 'Duplicate submission fixture');
                const { initialiseConversationDraft } = await import('/static/js/components/conversationDraft.js');
                const input = document.querySelector('#promptInput');
                initialiseConversationDraft({ input, sendButton: document.querySelector('#sendButton'), onSubmit: () => { window.pending = chat.sendMessage(); } });
                await chat.__testOnly_uploadFilesToVon([new File(['png'], 'fixture.png', { type: 'image/png' })]);
            });
            await page.locator('#promptInput').fill('Describe this image once');
            await page.locator('#sendButton').dblclick();
            await page.waitForFunction(() => generateBodies.length === 1);
            if (width > 800) await page.locator('#promptInput').press('Enter');
            else await page.locator('#sendButton').click();
            assert.deepEqual(await page.evaluate(() => [queueBodies.length, generateBodies.length]), [1, 1]);
            await page.screenshot({ path: path.join(output, `${width}-single-submission.png`) });
            await page.locator('#promptInput').fill('A deliberate follow-up');
            await page.locator('#sendButton').click();
            await page.waitForFunction(() => queueBodies.length === 2);
            assert.equal(await page.evaluate(() => queueBodies[1].prompt_raw), 'A deliberate follow-up');
            await page.evaluate(() => finishGenerate());
            assert.deepEqual(errors, []);
            evidence.push({ width, generated: 1, admittedOriginal: 1, admittedFollowUp: 1, errors });
            await page.close();
        }
        fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(evidence, null, 2));
        console.log(JSON.stringify(evidence));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
