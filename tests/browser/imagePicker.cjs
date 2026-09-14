// Local production-template/module fixture; no authentication or model requests.
// Run with PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/imagePicker.cjs .run/image-picker
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');

const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8')
    .replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const evidence = process.argv[2];
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/static/styles.css">${markup}`);
        return;
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) {
        res.writeHead(404).end();
        return;
    }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});


(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless:true});
    const results = [];
    try {
        for (const width of [360, 412, 1440]) {
            const page = await browser.newPage({viewport:{width, height:900}, isMobile:width < 800, hasTouch:width < 800});
            let uploads = 0;
            const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=', 'base64');
            await page.route('**/von/api/images/upload', route => {
                uploads++;
                return route.fulfill({json:{image_attachment:{concept_id:`#V#fixture-${uploads}`, filename:'phone.png'}}});
            });
            await page.route('**/von/api/images/*/original', route => route.fulfill({contentType:'image/png', body:png}));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                document.querySelector('#messagesContainer').style.display = 'none';
                document.querySelector('#scrollableField').textContent = 'Image picker acceptance draft';
                const images = await import('/static/js/utils/conversationImages.js');
                const { initialiseCompactChatComposer } = await import('/static/js/components/compactComposer.js');
                initialiseCompactChatComposer(document.querySelector('.chat-composer'));
                const panel = document.createElement('div'); panel.id = 'conversationImageComposer';
                document.querySelector('.chat-composer').append(panel);
                const render = () => images.renderImageComposer(panel, 'fixture', render);
                images.initialiseImagePicker(async files => {
                    for (const file of files) await images.uploadConversationImage(file, 'fixture', {}, render);
                }, message => {
                    document.querySelector('#uploadFileStatus').textContent = message;
                    document.querySelector('#chatAttachmentStatus').classList.remove('hidden');
                });
                window.fixtureImages = images;
            });
            const button = page.getByRole('button', {name:'Attach image', exact:true});
            await expect(button).toBeVisible();
            const box = await button.boundingBox();
            assert(box.width >= 44 && box.height >= 44);
            const chooser = page.waitForEvent('filechooser');
            await button.click();
            await (await chooser).setFiles({name:'phone.png', mimeType:'image/png', buffer:png});
            await expect(page.locator('#conversationImageComposer img')).toBeVisible();
            await expect(page.locator('#conversationImageComposer img')).toHaveJSProperty('naturalWidth', 1);
            await page.getByRole('button', {name:'Remove phone.png', exact:true}).click();
            await expect(page.locator('#conversationImageComposer img')).toHaveCount(0);
            await page.locator('#attachImageInput').setInputFiles({name:'replacement.png', mimeType:'image/png', buffer:png});
            await expect(page.locator('#conversationImageComposer img')).toBeVisible();
            const overflow = await page.locator('.chat-composer').evaluate(el => el.scrollWidth > el.clientWidth);
            assert(!overflow, `composer overflow at ${width}`);
            if (evidence) await page.screenshot({path:path.join(evidence, `${width}-preview.png`)});
            const ids = await page.evaluate(() => window.fixtureImages.takeImages('fixture'));
            assert.deepEqual(ids, ['#V#fixture-2']);
            results.push({width, uploads, ids, overflow});
            await page.close();
        }
        if (evidence) fs.writeFileSync(path.join(evidence, 'results.json'), JSON.stringify(results, null, 2));
        console.log(JSON.stringify(results));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
