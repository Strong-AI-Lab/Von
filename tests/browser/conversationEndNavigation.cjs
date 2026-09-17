// Isolated layout acceptance: production templates, CSS and chat navigation module.
// No authentication, live messages, database writes, or model calls are exercised.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/conversationEndNavigation.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/conversation-end';
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
        for (const width of [1440, 900]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                document.body.classList.remove('von-auth-pending');
                document.querySelector('#vonAuthenticationGate').hidden = true;
                document.querySelector('#vonAuthenticatedApp').hidden = false;
                window.chat = await import('/static/js/chatTab.js');
                const attachments = document.createElement('div');
                attachments.id = 'conversationImageComposer';
                document.querySelector('.chat-composer').append(attachments);
                window.field = document.querySelector('#scrollableField');
                field.innerHTML = Array.from({ length: 25 }, (_, i) =>
                    `<div class="message-container"><p>Contribution ${i + 1}</p><p>${'Research observations and next steps. '.repeat(20)}</p></div>`).join('');
                window.refresh = () => chat.__testOnly_updateScrollToEndButtonVisibility(field);
                window.addEventListener('scroll', refresh);
                window.addEventListener('resize', refresh);
                refresh();
            });
            const button = page.locator('#chatScrollToEndButton');
            for (const expanded of [false, true]) {
                await page.evaluate(expanded => {
                    document.querySelector('#conversationImageComposer').innerHTML = expanded
                        ? '<div style="height:500px">Two attached screenshot previews (fixture)</div>' : '';
                    window.scrollTo(0, 500);
                    refresh();
                }, expanded);
                await expect(button).toBeVisible();
                const before = await page.evaluate(() => {
                    const b = document.querySelector('#chatScrollToEndButton').getBoundingClientRect();
                    const c = document.querySelector('.chat-composer').getBoundingClientRect();
                    return { arrowTop: b.top, arrowBottom: b.bottom, composerTop: c.top, composerHeight: c.height };
                });
                await page.screenshot({ path: path.join(evidence, `${width}-${expanded ? 'attachments' : 'draft'}-history.png`) });
                assert(before.arrowTop >= 0 && before.arrowBottom <= 900, 'arrow reachable while reading');
                assert(before.arrowBottom <= before.composerTop, 'arrow on conversation above composer');
                await button.click();
                await expect(button).toBeHidden();
                const after = await page.evaluate(() => {
                    const f = field.getBoundingClientRect();
                    const c = document.querySelector('.chat-composer').getBoundingClientRect();
                    return { transcriptBottom: f.bottom, composerTop: c.top, composerHeight: c.height, scrollY,
                        pageBottomGap: document.documentElement.scrollHeight - innerHeight - scrollY };
                });
                assert(after.transcriptBottom > 100 && after.transcriptBottom <= after.composerTop, 'latest contribution visible');
                assert.equal(after.composerHeight, before.composerHeight, 'visibility does not resize composer');
                results.push({ width, expanded, before, after });
                await page.screenshot({ path: path.join(evidence, `${width}-${expanded ? 'attachments' : 'draft'}-end.png`) });
            }
            await page.evaluate(() => {
                field.innerHTML = '<div class="message-container"><p>Short conversation</p></div>';
                document.querySelector('#conversationImageComposer').replaceChildren();
                chat.__testOnly_scrollConversationToEnd(field, { smooth: false });
                refresh();
            });
            await expect(button).toBeHidden();
            await page.setViewportSize({ width: 390, height: 844 });
            await page.evaluate(() => refresh());
            assert.equal(await button.evaluate(el => el.parentElement.className), 'chat-composer');
            await page.setViewportSize({ width, height: 900 });
            await page.evaluate(() => refresh());
            assert.equal(await button.evaluate(el => el.parentElement.className), 'chat-transcript-navigation');
            await expect(button).toHaveCount(1);
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'measurements.json'), JSON.stringify(results, null, 2));
        console.log(JSON.stringify(results, null, 2));
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
