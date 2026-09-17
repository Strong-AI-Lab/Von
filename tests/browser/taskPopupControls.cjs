/** Tier 1 fixture: candidate task component, template and CSS; synthetic read-only tasks. */
const { chromium, expect } = require('@playwright/test');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve('src/frontend/web/von_interface/static');
const output = path.resolve(process.argv[2] || '.run/task-popup-controls');
const task = { task_concept_id: '#V#popup_fixture', title: 'Completed research task with a long title', description: 'long_unbroken_research_reference_'.repeat(12), status: 'completed', priority: 'medium' };
const template = fs.readFileSync('src/frontend/web/von_interface/templates/chat_tab.html', 'utf8');
const popup = template.slice(template.indexOf('  <div id="taskPanel"'), template.indexOf('  <div id="taskPanel"') + template.slice(template.indexOf('  <div id="taskPanel"')).indexOf('\n  </div>') + 9);
const stubs = {
    '/js/workflowStudioAccess.js': 'export const canUseWorkflowStudio=()=>false; export const setupWorkflowStudioAccess=()=>{}; ',
    '/js/conceptTab.js': 'export const fetchConceptList=()=>{}; export const initializeConceptTab=()=>{}; export const updateConceptTabUI=()=>{};',
    '/js/dynamicTabs.js': 'export const initializeDynamicTabs=()=>{}; export const loadDynamicConceptTabContent=()=>{};',
    '/js/importExportTab.js': 'export const initializeImportExportTab=()=>{};',
    '/js/vontology.js': 'export const initializeVontologyTab=()=>{};',

    '/js/apiService.js': `export const getWindowSessionId=()=> 'fixture'; export const WINDOW_SESSION_HEADER='X-Von-Window-Session'; export const ensureUniqueWindowSessionId=async()=> 'fixture'; export const getUserContext=()=>({user_id:'#V#fixture',org_id:'#V#fixture_org'}); export const getJson=async url=>(await fetch(url)).json(); export const getJsonDetailed=async url=>({data:await getJson(url)}); export const postJson=()=>{throw Error('Unexpected write')}; export const patchJson=postJson; export const deleteJson=postJson;`,
    '/js/utils/toast.js': 'export const showToast=()=>{};',
    '/js/markdownUtils.js': 'export const renderMarkdownViaServer=async()=>"";',
};
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') {
        res.setHeader('Content-Type', 'text/html');
        res.end(`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><button id="opener">Task reference</button><button id="outside">Outside</button>${popup}<button class="tab-button" data-tab="globalTasksTab">Tasks</button><div id="globalTasksTab" class="tab-content"><div id="globalTasksContainer"></div></div><script type="module">import {openTaskInPanel} from '/js/components/taskPanel.js'; document.querySelector('#opener').onclick=()=>openTaskInPanel('#V#popup_fixture');</script></body></html>`);
    } else if (stubs[url.pathname]) {
        res.setHeader('Content-Type', 'text/javascript'); res.end(stubs[url.pathname]);
    } else if (url.pathname.startsWith('/api/')) {
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify(decodeURIComponent(url.pathname) === '/api/tasks/#V#popup_fixture' ? task : url.pathname === '/api/tasks/' ? { tasks: [] } : {}));
    } else {
        const file = path.join(root, url.pathname);
        if (!file.startsWith(root + '/') || !fs.existsSync(file)) { res.statusCode = 404; res.end(); return; }
        res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
        res.end(fs.readFileSync(file));
    }
});
(async () => {
    fs.mkdirSync(output, { recursive: true });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let browser;
    const measurements = [];
    try {
        browser = await chromium.launch();
        const page = await browser.newPage();
        const errors = []; page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
        for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 568 }]) {
            await page.setViewportSize(viewport);
            await page.goto(`http://127.0.0.1:${server.address().port}/`);
            await page.getByRole('button', { name: 'Task reference', exact: true }).click();
            const panel = page.getByRole('dialog', { name: 'Tasks', exact: true });
            await expect(panel.locator('.task-detail-toggle-btn')).toHaveAttribute('aria-expanded', 'true');
            const before = await panel.boundingBox();
            await page.getByRole('button', { name: 'Hide details', exact: true }).click();
            const details = page.getByRole('button', { name: 'Show details', exact: true });
            await expect(details).toHaveAttribute('aria-expanded', 'false');
            await expect(details).toBeFocused();
            const after = await panel.boundingBox();
            assert(Math.abs(before.height - after.height) < 1 && Math.abs(before.width - after.width) < 1, 'details do not resize popup');
            const overflow = await panel.evaluate(el => el.scrollWidth - el.clientWidth);
            const bodyOverflow = await panel.locator('.llm-popup-body').evaluate(el => el.scrollWidth - el.clientWidth);
            assert(before.x >= 0 && before.x + before.width <= viewport.width + 1 && before.y + before.height <= viewport.height, 'popup stays inside viewport');
            assert(overflow <= 1 && bodyOverflow <= 1, 'no horizontal clipping');
            await details.press('Enter');
            await expect(panel.locator('.task-detail-toggle-btn')).toHaveAttribute('aria-expanded', 'true');
            await expect(panel.locator('.task-detail-loading')).toHaveCount(0);
            await expect(panel.locator('.task-detail-toggle-btn')).toBeFocused();
            await page.screenshot({ path: path.join(output, `popup-${viewport.width}.png`) });
            await page.keyboard.press('Escape');
            await expect(panel).toBeHidden();
            await expect(page.locator('#opener')).toBeFocused();
            await page.locator('#opener').click();
            await page.getByRole('button', { name: 'Close Tasks popup' }).press('Space');
            await expect(panel).toBeHidden();
            await page.locator('#opener').click();
            await page.locator('#outside').click();
            await expect(panel).toBeHidden();
            await page.locator('#opener').click();
            await page.getByRole('button', { name: 'Open in Tasks', exact: true }).press('Enter');
            await expect(panel).toBeHidden();
            await expect(page.locator('#globalTaskInspector')).toContainText(task.title);
            await expect(page.locator('#globalTaskInspector')).toBeFocused();
            assert.equal(await page.evaluate(() => location.hash), '#globalTasksTab');
            measurements.push({ viewport, before, after, overflow, bodyOverflow });
        }
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output, 'measurements.json'), JSON.stringify(measurements, null, 2));
        console.log(JSON.stringify(measurements));
    } finally { await browser?.close(); server.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
