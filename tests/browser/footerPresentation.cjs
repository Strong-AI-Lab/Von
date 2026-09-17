// Candidate-source browser fixture: no backend, credentials, or model requests.
const { chromium, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const template = fs.readFileSync(path.join(root, 'templates/von_interface.html'), 'utf8');
const footer = template.slice(template.indexOf('  <!-- Footer for Model Info -->'), template.indexOf('  <!-- RAG Status Modal -->')).replace(/{%.*?%}/g, '');
const settings = fs.readFileSync(path.join(root, 'templates/settings_tab.html'), 'utf8').match(/<section id="footer-settings"[\s\S]*?<\/section>/)[0];
const html = `<link rel="stylesheet" href="/static/styles.css">${settings}${footer}<script type="module">
import { setModelInfoFooterText } from '/static/js/domUtils.js';
import { mountFooterPreferences } from '/static/js/utils/footerPreferences.js';
localStorage.setItem('von_current_user', JSON.stringify({name:'Footer Test User', concept_id:'#V#footer_test'}));
const summary = { estimated_cost: {status:'estimated', currency:'USD', amount:0.014889} };
window.__vonConversationRuntimeCostSnapshot = {context:{conversation_session_id:'footer-fixture'}, baseline:{conversation:summary,since_restart:summary}};
await setModelInfoFooterText();
mountFooterPreferences();
document.querySelector('#serverBuildInfo').hidden = false;
document.querySelector('#serverBuildValue').textContent = 'abcdef123456 2026-09-13T16:40:50.344900+00:00';
window.fixtureReady = true;
</script>`;
const server = http.createServer((req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); return res.end(html); }
    if (!req.url.startsWith('/static/') || req.url.includes('..')) { res.writeHead(404); return res.end(); }
    const file = path.join(root, req.url.split('?')[0]);
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
    try { res.end(fs.readFileSync(file)); } catch { res.writeHead(404); res.end(); }
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless:true});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => {
        const url = route.request().url();
        let body = {};
        if (/\/settings\/?$/.test(url)) body = {active_llm:{model:'gpt-6-astra',provider:'openai'}};
        if (url.includes('/auth/status')) body = {authenticated:true,email:'footer@example.test'};
        if (url.includes('capability-index/status')) body = {ready:true,status:'ready'};
        if (url.includes('my_organisations')) body = {organisations:[{concept_id:'#V#footer_org',name:'Footer Fixture Organisation',role:'member'}]};
        return route.fulfill({json:body});
    });
    const evidence = [];
    try {
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.waitForFunction(() => window.fixtureReady);
        await expect(page.locator('#serverUptimeFooter')).toBeHidden();
        await expect(page.locator('#languageIndicator')).toBeHidden();
        await expect(page.locator('[data-cost-scope="conversation"]')).toContainText('US$0.01');
        for (const checkbox of await page.locator('[data-footer-preference]').all()) await checkbox.check();
        await expect(page.locator('#serverBuildInfo')).toBeVisible();
        await page.reload();
        await page.waitForFunction(() => window.fixtureReady);
        await expect(page.locator('[data-footer-preference="build"]')).toBeChecked();
        for (const width of [1297, 493, 320]) {
            await page.setViewportSize({width,height:800});
            await expect(page.locator('#serverBuildInfo')).toBeVisible();
            const measurement = await page.locator('.footer-container').evaluate(el => {
                const items = [...el.children].filter(child => getComputedStyle(child).display !== 'none');
                const centres = items.map(child => { const r = child.getBoundingClientRect(); return r.y+r.height/2; });
                el.scrollLeft = el.scrollWidth;
                return {width:innerWidth, clientWidth:el.clientWidth, scrollWidth:el.scrollWidth, scrollLeft:el.scrollLeft, centreSpread:Math.max(...centres)-Math.min(...centres), height:el.getBoundingClientRect().height};
            });
            expect(measurement.centreSpread).toBeLessThan(2);
            if (width <= 493) expect(measurement.scrollLeft).toBeGreaterThan(0);
            evidence.push(measurement);
            await page.locator('.footer-container').evaluate(el => { el.scrollLeft = 0; });
            await page.locator('[data-cost-scope="conversation"]').click();
            const panel = page.locator('#modelInfoFooter .conversation-runtime-cost-details');
            await expect(panel).toBeVisible();
            const box = await panel.boundingBox();
            expect(box.x).toBeGreaterThanOrEqual(0);
            expect(box.x + box.width).toBeLessThanOrEqual(width);
            expect(await panel.evaluate(el => {const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+5,r.y+5));})).toBe(true);
            await page.locator('[data-cost-scope="conversation"]').click();
        }
        await page.locator('.footer-org-menu-trigger').click();
        const organisationMenu = page.locator('.footer-org-menu');
        await expect(organisationMenu).toBeVisible();
        expect(await organisationMenu.evaluate(el => {const r=el.getBoundingClientRect();return el.contains(document.elementFromPoint(r.x+5,r.y+5));})).toBe(true);
        await page.locator('.footer-org-menu-trigger').click();
        // The responsive shell reparents the same footer into its details panel.
        await page.evaluate(() => {
            const panel = document.createElement('div');
            panel.className = 'mobile-footer-details-panel';
            document.body.append(panel);
            panel.append(document.querySelector('.footer-container'));
        });
        const mobile = await page.locator('.footer-container').evaluate(el => ({height:el.getBoundingClientRect().height, width:el.clientWidth, scrollWidth:el.scrollWidth, direction:getComputedStyle(el).flexDirection}));
        expect(mobile.direction).toBe('row');
        expect(mobile.scrollWidth).toBeGreaterThan(mobile.width);
        expect(mobile.height).toBeLessThan(80);
        evidence.push({mobileDetails:mobile});
        await page.locator('[data-footer-preference="conversationCost"]').uncheck();
        await expect(page.locator('[data-cost-scope="conversation"]')).toBeHidden();
        expect(errors).toEqual([]);
        const output = process.argv[2] || '.run/footer-presentation';
        fs.mkdirSync(output,{recursive:true});
        await page.screenshot({path:path.join(output,'footer-320.png'),fullPage:true});
        fs.writeFileSync(path.join(output,'measurements.json'), JSON.stringify({fixture:'candidate source with mocked API reads',evidence},null,2));
        console.log(JSON.stringify(evidence));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
