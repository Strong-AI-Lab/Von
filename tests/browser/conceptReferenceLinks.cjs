// Isolated browser fixture: real renderer, annotator and navigation handler;
// concept metadata and terminal tab/panel dependencies are fixture-backed.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface/static');
const ids = ['#V#task_agent_65d772a220b888b99f69da12723eafab', '#V#masataro_asai'];
const output = path.resolve(process.argv[2] || '.run/concept-reference-links');
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/von/') {
        res.setHeader('Content-Type', 'text/html');
        res.end(`<!doctype html><link rel="stylesheet" href="/styles.css"><div class="chat-markdown markdown-rendered" id="chat"></div><output id="opened"></output><script type="module">
import {simpleMarkdownToHtml} from '/js/markdownUtils.js';
import {cartouchifyVontologyTokensInElement} from '/js/utils/textDecorator.js';
import {handleSelectConceptByIdDetail} from '/js/utils/selectConceptByIdHandler.js';
const ids = ${JSON.stringify(ids)};
const chat = document.querySelector('#chat');
chat.innerHTML = simpleMarkdownToHtml(ids.map(id => '[Concept](https://von.curiouscat.cc/von/' + id + ')').join('\\n') + '\\n[External source](https://example.com/paper#V#masataro_asai)');
cartouchifyVontologyTokensInElement(chat);
window.requests = [];
document.addEventListener('von:selectConceptById', async event => {
    await handleSelectConceptByIdDetail(event.detail, {
        fetchFn: async url => {
            const id = new URL(url, location.href).searchParams.get('identifier');
            window.requests.push(id);
            if (!ids.includes(id)) throw Error('Unexpected identity');
            return {ok: true, json: async () => ({display_name: id === ids[1] ? 'Masataro Asai' : 'Task fixture', kind: 'individual', raw_doc: {relationships: {is_an_instance_of: id === ids[0] ? ['#V#task_specification'] : ['#V#person']}}})};
        },
        createOrActivateConceptTab: (id, name) => { document.querySelector('#opened').textContent = 'concept:' + id + ':' + name; },
        closeDynamicConceptTab: () => {},
        openTaskPanel: async id => { document.querySelector('#opened').textContent = 'task:' + id; }
    });
});
</script>`);
    } else {
        const file = path.resolve(root, '.' + url.pathname);
        if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) { res.writeHead(404); res.end(); return; }
        res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
        res.end(fs.readFileSync(file));
    }
});
(async () => {
    let browser;
    try {
        fs.mkdirSync(output, {recursive: true});
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        browser = await chromium.launch({headless: true});
        const page = await browser.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/von/`);
        await page.locator('.vontology-cartouche').first().waitFor();
        for (const [index, id] of ids.entries()) {
            await page.locator('.vontology-cartouche').nth(index).click();
            const expected = index === 0 ? 'task:' + id : 'concept:' + id + ':Masataro Asai';
            await page.waitForFunction(text => document.querySelector('#opened').textContent === text, expected);
        }
        assert.equal(await page.locator('a').getAttribute('href'), 'https://example.com/paper#V#masataro_asai');
        assert.equal(await page.locator('a').getAttribute('target'), '_blank');
        assert.deepEqual(await page.evaluate(() => window.requests), [ids[0], ids[0], ids[1], ids[1]]);
        assert.deepEqual(errors, []);
        await page.screenshot({path: path.join(output, 'concept-links.png')});
        const receipt = {fixture: true, canonicalDataVerified: false, ids, navigation: ['task panel', 'concept tab'], externalLinkPreserved: true, pageErrors: errors};
        fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2));
        console.log(JSON.stringify(receipt));
    } finally {
        if (browser) await browser.close();
        server.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
