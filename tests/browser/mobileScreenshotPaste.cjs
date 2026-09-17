// Production composer/module fixture, real Chromium clipboard, stubbed HTTP.
// Native Android permission/keyboard evidence is separately retained on the task.
// Run: PLAYWRIGHT_BROWSERS_PATH=... node tests/browser/mobileScreenshotPaste.cjs [evidence-directory]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8')
    .replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const png = fs.readFileSync(path.join(__dirname, 'fixtures/attachment.png'));
const evidence = process.argv[2];
if (evidence) fs.mkdirSync(evidence, {recursive:true});
let uploads = [];
const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/styles.css">${markup}`);
        return;
    }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url.split('?')[0]}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
            res.end(fs.readFileSync(file)); return;
        }
    }
    if (req.url === '/fixture.png' || req.url.endsWith('/original')) {
        res.setHeader('Content-Type', 'image/png'); res.end(png); return;
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/von/api/images/upload') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        const body = Buffer.concat(chunks);
        assert(body.includes(Buffer.from('Content-Type: image/png')));
        assert(body.includes(Buffer.from([137, 80, 78, 71])));
        uploads.push({bytes:body.length, windowSession:!!req.headers['x-von-window-session']});
        res.end(JSON.stringify({image_attachment:{concept_id:`#V#fixture-image-${uploads.length}`,filename:'screenshot.png'}})); return;
    }
    res.end(JSON.stringify({authenticated:true, success:true, sessions:[], messages:[], items:[], history:[], history_length:0, organisations:[]}));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless:true});
    const results = [];
    try {
        for (const width of [360, 448, 1440]) {
            uploads = [];
            const context = await browser.newContext({viewport:{width,height:891},hasTouch:width<800,isMobile:width<800,permissions:['clipboard-read','clipboard-write']});
            const page = await context.newPage();
            const errors = [];
            page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                document.querySelector('#messagesContainer').style.display = 'none';
                window.chat = await import('/static/js/chatTab.js');
                window.chat.initializeChatTab();
                window.chat.__testOnly_setActiveChatSession('clipboard-fixture', 'Clipboard fixture');
            });
            const input = page.locator('#promptInput');
            const caption = 'Describe this screenshot\nKeep both lines';
            await input.fill(caption);
            await page.locator('.chat-composer-more-actions>summary').click();
            const paste = page.getByRole('button', {name:'Paste image',exact:true});
            await expect(paste).toBeVisible();
            await page.evaluate(async () => {
                const blob = await (await fetch('/fixture.png')).blob();
                await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
            });
            await paste.click();
            await expect.poll(() => uploads.length).toBe(1);
            await expect(page.locator('#conversationImageComposer img')).toBeVisible();
            await expect(input).toHaveValue(caption);
            assert(uploads[0].windowSession);
            await page.getByRole('button', {name:'Remove pasted-image-1.png',exact:true}).click();
            await expect(page.locator('#conversationImageComposer img')).toHaveCount(0);
            // Denied browser clipboard access offers recovery without changing the caption.
            await context.clearPermissions();
            const cdp = await context.newCDPSession(page);
            await cdp.send('Browser.setPermission', {permission:{name:'clipboard-read'}, setting:'denied', origin:new URL(page.url()).origin});
            if (!await page.locator('.chat-composer-more-actions').evaluate(el => el.open)) {
                await page.locator('.chat-composer-more-actions>summary').click();
            }
            await paste.click();
            await expect(page.locator('#uploadFileStatus')).toContainText('Use Attach image');
            await expect(input).toHaveValue(caption);
            const chooser = page.waitForEvent('filechooser');
            await page.getByRole('button', {name:'Attach image',exact:true}).click();
            await (await chooser).setFiles({name:'picker.png',mimeType:'image/png',buffer:png});
            await expect.poll(() => uploads.length).toBe(2);
            await expect(page.locator('#conversationImageComposer img')).toBeVisible();
            await expect(input).toHaveValue(caption);
            const overflow = await page.locator('.chat-composer').evaluate(el => el.scrollWidth > el.clientWidth);
            assert(!overflow, `composer overflow at ${width}`);
            if (evidence) await page.screenshot({path:path.join(evidence, `${width}-recovery.png`)});
            await page.locator('.chat-composer-more-actions>summary').click();
            await page.locator('#conversationImageComposer img').scrollIntoViewIfNeeded();
            if (evidence) await page.screenshot({path:path.join(evidence, `${width}-preview.png`)});
            // Real desktop clipboard insertion verifies that the unchanged textarea
            // listener leaves ordinary multiline text to the browser.
            await context.grantPermissions(['clipboard-read','clipboard-write']);
            await page.evaluate(() => navigator.clipboard.writeText('Plain text\nSecond line'));
            await input.fill(''); await input.focus(); await page.keyboard.press('Control+V');
            await expect(input).toHaveValue('Plain text\nSecond line');
            assert.equal(uploads.length, 2);
            assert.deepEqual(errors, []);
            results.push({width, uploads, overflow, captionPreserved:true, deniedRecovery:true, multilineTextPaste:true, errors});
            await context.close();
        }
        const report = {browser:await browser.version(), scope:'Production template and initializeChatTab, real desktop clipboard with test grants/denial, HTTP upload stubs; native Android capability evidence is separate', results};
        if (evidence) fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify(report,null,2));
        console.log(JSON.stringify(report));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
