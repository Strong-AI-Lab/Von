/** Actual task-panel module and CSS with isolated API fixtures; no live data or models. */
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
    const output = path.resolve(process.argv[2] || '.run/task-board-status-selection');
    fs.mkdirSync(output, { recursive: true });
    const root = path.resolve('src/frontend/web/von_interface/static');
    const task = { task_concept_id: '#V#task_fixture', title: 'Delivered DGX task', status: 'deployed', priority: 'medium', assignee_concept_id: '#V#codex_dgx' };
    const modules = {
        '/js/apiService.js': `export const WINDOW_SESSION_HEADER='X-Von-Window-Session'; export const ensureUniqueWindowSessionId=async()=> 'fixture'; export const getUserContext=()=>({user_id:'#V#fixture',org_id:'#V#fixture_org'}); export const getJson=async(url)=>{if(url.startsWith('/api/tasks/?'))return {tasks:[${JSON.stringify(task)}]}; if(url==='/api/tasks/%23V%23task_fixture')return ${JSON.stringify(task)};return {};}; export const postJson=async()=>({});export const patchJson=async()=>({});export const deleteJson=async()=>({});`,
        '/js/tabNavigation.js': 'export const activateTab=()=>{};',
        '/js/utils/toast.js': 'export const showToast=()=>{};',
        '/js/markdownUtils.js': 'export const renderMarkdownViaServer=async()=>"";',
        '/js/utils/nameSelection.js': 'export const selectBestNameForContext=()=>null;',
        '/js/components/taskRunActivity.js': 'export const openTaskRunActivity=()=>{};',
    };
    const server = http.createServer((req, res) => {
        if (req.url === '/') {
            res.setHeader('Content-Type', 'text/html');
            res.end('<!doctype html><html><head><link rel="stylesheet" href="/styles.css"></head><body><div id="globalTasksContainer"></div><script type="module">import {showGlobalTasks} from "/js/components/taskPanel.js";await showGlobalTasks();</script></body></html>');
        } else if (modules[req.url]) {
            res.setHeader('Content-Type', 'text/javascript');res.end(modules[req.url]);
        } else if (['/styles.css', '/js/components/taskPanel.js'].includes(req.url)) {
            res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'text/javascript');res.end(fs.readFileSync(path.join(root, req.url)));
        } else {res.statusCode=404;res.end();}
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let browser;
    try {
        browser = await chromium.launch({headless:true, args:['--no-sandbox']});
        const page = await browser.newPage();
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.locator('.task-item').waitFor();
        for (const width of [1440, 390]) {
            await page.setViewportSize({width,height:900});
            assert.equal(await page.locator('.task-group').count(),6);
            assert.match(await page.locator('.task-item').innerText(),/Delegated to Codex DGX/);
            await page.getByRole('button',{name:'Close task details',exact:true}).click();
            assert.equal(await page.locator('.task-item.is-selected').count(),0);
            assert.equal(await page.locator('.task-item').evaluate(el=>el===document.activeElement),true);
            await page.keyboard.press('Enter');
            await page.getByRole('button',{name:'Close task details',exact:true}).waitFor();
            await page.getByRole('button',{name:'Close task details',exact:true}).focus();
            await page.keyboard.press('Escape');
            assert.equal(await page.locator('.task-item.is-selected').count(),0);
            await page.selectOption('#globalTaskStatusFilter','pending');
            assert.equal(await page.locator('.task-item').count(),0);
            await page.selectOption('#globalTaskStatusFilter','deployed');
            assert.equal(await page.locator('.task-item').count(),1);
            await page.locator('.task-item').focus();await page.keyboard.press('Enter');
            await page.screenshot({path:path.join(output,`board-${width}.png`),fullPage:true});
        }
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({passed:true,widths:[1440,390],checks:['six status lanes','DGX delegation','clear and keyboard reopen','Escape and focus restoration','deployed filtering'],scope:'isolated API fixture using candidate taskPanel.js and styles.css'},null,2));
        console.log('Task board browser fixture passed');
    } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
