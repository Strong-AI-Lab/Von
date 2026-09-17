// Exercise the production avatar module, styles and real local brand assets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2];
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css"><main style="padding:24px"><h2>Imported conversations</h2></main>');
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    const type = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' }[path.extname(file)];
    res.setHeader('Content-Type', type || 'text/plain');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage({ viewport: { width: 760, height: 600 } });
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.evaluate(async () => {
            const { importedConversationAvatar: avatar } = await import('/static/js/components/importedConversationAvatar.js');
            for (const [provider, label] of [['codex', 'Codex'], ['claude_code', 'Claude Code'], ['copilot', 'GitHub Copilot'], ['gemini', 'Gemini'], ['unknown', 'Other agent']]) {
                const row = document.createElement('div');
                row.style.cssText = 'display:flex;align-items:center;gap:16px;margin:20px 0';
                row.append(avatar(provider, label, { sidebar: true }), avatar(`${provider}:agent`, label), document.createTextNode(label));
                document.querySelector('main').append(row);
            }
        });
        await page.waitForFunction(() => [...document.images].every(image => image.complete && image.naturalWidth > 0));
        assert.equal(await page.locator('.has-provider-logo').count(), 8);
        assert.equal(await page.locator('[aria-label="Other agent (imported)"] img').count(), 0);
        assert.equal(await page.locator('.participant-avatar').first().evaluate(el => el.getBoundingClientRect().width), 32);
        assert.equal(await page.locator('.chat-imported-actor-avatar').first().evaluate(el => el.getBoundingClientRect().width), 40);
        if (evidence) { fs.mkdirSync(evidence, { recursive: true }); await page.screenshot({ path: path.join(evidence, 'imported-provider-logos.png') }); }
        await page.locator('img').first().evaluate(image => image.dispatchEvent(new Event('error')));
        assert.equal(await page.locator('.participant-avatar').first().innerText(), 'C');
        console.log('Provider logos loaded at sidebar/message sizes; unknown and failed-image fallbacks verified.');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
