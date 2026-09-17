// Candidate template/CSS/modules with synthetic read-only API responses.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/searchContentTypes.cjs [evidence-dir]
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const template = fs.readFileSync(path.join(root, 'templates/von_interface.html'), 'utf8');
const header = template.slice(template.indexOf('<div class="global-concept-search"'), template.indexOf('<div class="main-container global-organisation-identity"'));
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end(`<link rel="stylesheet" href="/static/styles.css"><header class="global-header"><div class="global-header-shell">${header}</div></header>`);
    }
    const file = path.resolve(root, `.${req.url.split('?')[0]}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage();
        await page.route('**/*', route => {
            if (!new URL(route.request().url()).pathname.includes('/api/')) return route.continue();
            return route.fulfill({ json: { results: [], tasks: [], conversations: [] } });
        });
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        await page.evaluate(async () => {
            const { elements } = await import('/static/js/domUtils.js');
            elements.vontologySearchInput = document.querySelector('#vontologySearchInput');
            elements.vontologySearchResults = document.querySelector('#vontologySearchResults');
            (await import('/static/js/vontology.js')).setupVontologySearchUI();
        });
        await page.locator('#vontologySearchInput').fill('research');
        await expect(page.locator('.unified-search-group')).toHaveCount(3);
        for (const width of [1000, 375]) {
            await page.setViewportSize({ width, height: 800 });
            const summary = page.locator('#searchContentTypes summary');
            await summary.click();
            await page.getByRole('checkbox', { name: 'Concepts', exact: true }).uncheck();
            await expect(page.locator('.unified-search-group-concepts')).toHaveCount(0);
            await expect(summary).toHaveCSS('background-color', 'rgb(254, 243, 199)');
            const bounds = await page.locator('.search-content-types-menu').boundingBox();
            if (bounds.x < 0 || bounds.x + bounds.width > width) throw new Error('Dropdown exceeds viewport');
            if (process.argv[2]) {
                fs.mkdirSync(process.argv[2], { recursive: true });
                await page.screenshot({ path: path.join(process.argv[2], `search-types-${width}.png`) });
            }
            await page.getByRole('checkbox', { name: 'Conversations', exact: true }).uncheck();
            await page.getByRole('checkbox', { name: 'Tasks', exact: true }).uncheck();
            await expect(page.locator('#vontologySearchResults')).toContainText('Select at least one content type');
            for (const label of ['Concepts', 'Conversations', 'Tasks']) await page.getByRole('checkbox', { name: label, exact: true }).check();
            await expect(summary).toHaveCSS('background-color', 'rgb(255, 255, 255)');
            await page.getByRole('checkbox', { name: 'Tasks', exact: true }).press('Escape');
            await expect(summary).toBeFocused();
            await expect(page.locator('#searchContentTypes')).not.toHaveAttribute('open');
        }
        console.log('PASS: candidate header at 1000px and 375px; filters, empty selection, colour, bounds and Escape focus (synthetic APIs).');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
