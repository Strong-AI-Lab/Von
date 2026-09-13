// Candidate catalogue module with synthetic API replies; no login or live data.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/conversationTopicSearch.cjs [evidence-dir]
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2];
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end('<link rel="stylesheet" href="/static/styles.css"><div class="conversation-tray-header">Conversations</div><div id="results"></div><div id="controls"></div><textarea id="draft">Retained draft</textarea>');
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
        let searches = 0;
        await page.route('**/*', route => {
            const url = new URL(route.request().url());
            if (!url.pathname.includes('/api/')) return route.continue();
            if (url.pathname.endsWith('/conversation_search')) {
                searches++;
                return route.fulfill({ json: { success: true, coverage_complete: true, results: [
                    { session_id: 'semantic', session_name: 'Passive ventilation', last_message_at: '2020-01-01', match: { fields: ['semantic_content'], score: 1000 } },
                    { session_id: 'title', session_name: 'Cooling buildings', match: { fields: ['display_name', 'semantic_content'], score: 1 } }
                ] } });
            }
            return route.fulfill({ json: { conversations: [], profiles: [], success: true } });
        });
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        await page.evaluate(async () => {
            const catalogue = await import('/static/js/components/conversationCatalogue.js');
            const rows = [
                { session_id: 'title', session_name: 'Cooling buildings' },
                { session_id: 'participant', session_name: 'Facilities', participant_ids: ['#V#cooling_buildings_team'] },
                { session_id: 'unrelated', session_name: 'Shopping' }
            ];
            const render = () => {
                const results = document.getElementById('results');
                results.replaceChildren(...catalogue.filterCatalogueRows(rows).map(row => {
                    const button = document.createElement('button');
                    button.dataset.sessionId = row.session_id;
                    button.textContent = row.session_name;
                    return button;
                }));
                const controls = document.getElementById('controls'); controls.replaceChildren();
                catalogue.mountCatalogueControls(controls);
            };
            catalogue.initialiseConversationCatalogue({ render });
            render();
        });
        const input = page.getByRole('searchbox');
        await input.fill('cooling buildings');
        await expect(page.locator('#results button')).toHaveText(['Cooling buildings', 'Facilities', 'Passive ventilation']);
        await expect(page.locator('#draft')).toHaveValue('Retained draft');
        if (searches !== 1) throw new Error(`Expected one debounced search, got ${searches}`);
        if (evidence) {
            fs.mkdirSync(evidence, { recursive: true });
            await page.screenshot({ path: path.join(evidence, 'conversation-topic-search.png') });
        }
        await input.fill('');
        await expect(page.locator('#results button')).toHaveText(['Cooling buildings', 'Facilities', 'Shopping']);
        console.log('PASS: candidate catalogue module, metadata-first topic results, old unloaded semantic hit, clear and retained draft (synthetic API).');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
