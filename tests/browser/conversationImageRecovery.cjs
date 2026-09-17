// Isolated component fixture only: no authenticated backend or recipient delivery.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

async function main() {
    const evidence = path.resolve(process.argv[2] || '.run/image-recovery');
    await fs.mkdir(evidence, { recursive: true });
    const source = await fs.readFile(path.resolve(__dirname,
        '../../src/frontend/web/von_interface/static/js/utils/conversationImages.js'));
    const server = http.createServer((req, res) => {
        if (req.url === '/composer.js') {
            res.setHeader('Content-Type', 'text/javascript'); res.end(source);
        } else {
            res.setHeader('Content-Type', 'text/html');
            res.end('<!doctype html><meta name="viewport" content="width=device-width"><main id="composer"></main>');
        }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let browser;
    try {
        browser = await chromium.launch({ headless: true });
        const results = [];
        for (const width of [1280, 360]) {
            const page = await browser.newPage({ viewport: { width, height: 800 } });
            let uploads = 0;
            await page.route('**/von/api/images/upload', async route => {
                uploads += 1;
                await route.fulfill({ status: 503, contentType: 'application/json',
                    body: JSON.stringify({ error: 'Fixture upload unavailable; retry retained.' }) });
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async () => {
                const api = await import('/composer.js');
                window.imageFixture = api;
                const canvas = document.createElement('canvas');
                canvas.width = canvas.height = 20;
                canvas.getContext('2d').fillRect(0, 0, 20, 20);
                const blob = await new Promise(resolve => canvas.toBlob(resolve));
                const file = new File([blob], 'Māori diagram (original).png', { type: 'image/png' });
                const changed = () => api.renderImageComposer(document.getElementById('composer'), 'fixture', changed);
                await api.uploadConversationImage(file, 'fixture', {}, changed);
            });
            assert.equal(await page.locator('#composer img').evaluate(img => img.complete && img.naturalWidth > 0), true);
            assert.equal(await page.locator('[role="alert"]').count(), 1);
            await page.getByRole('button', { name: 'Retry upload of Māori diagram (original).png', exact: true }).click();
            await page.getByRole('button', { name: 'Retry upload of Māori diagram (original).png', exact: true }).waitFor();
            assert.equal(uploads, 2);
            assert.equal(await page.evaluate(() => window.imageFixture.imageItems('fixture').length), 1);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
            await page.screenshot({ path: path.join(evidence, `${width}-retry.png`) });
            await page.getByRole('button', { name: 'Remove Māori diagram (original).png', exact: true }).click();
            assert.equal(await page.evaluate(() => window.imageFixture.imageItems('fixture').length), 0);
            results.push({ width, uploads, preview: true, retryRetainsOneFile: true, removed: true, overflow: false });
            await page.close();
        }
        await fs.writeFile(path.join(evidence, 'result.json'), JSON.stringify({ fixtureOnly: true, results }, null, 2));
        console.log(JSON.stringify({ fixtureOnly: true, results }));
    } finally {
        await browser?.close();
        await new Promise(resolve => server.close(resolve));
    }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
