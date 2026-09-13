/** Candidate task UI and styles with isolated deployment receipts; no live state or models. */
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
    const output = path.resolve(process.argv[2] || '.run/deployment-evidence');
    fs.mkdirSync(output, {recursive: true});
    const root = path.resolve('src/frontend/web/von_interface/static');
    const deployments = ['deployed', 'verified', 'rolled_back'].map((status, index) => ({
        concept_id: `#V#deployment_${index}`, deployment_id: `dgx-attempt-${index}`,
        environment: 'production', target: 'Von service / PWA', status,
        deployed_at: '2026-09-12T21:59:00Z',
        build: {build_id: `sha256:${'a'.repeat(64)}`, source_revision: 'ab10a303e'},
        observations: [{status, evidence: 'Representative fixture receipt; not a live deployment verification'}],
    }));
    const task = {task_concept_id:'#V#task_fixture', title:'Represent deployed Von builds', status:'in_progress', priority:'medium', deployments};
    const modules = {
        '/js/apiService.js': `let task=${JSON.stringify(task)};export const WINDOW_SESSION_HEADER='X-Von-Window-Session';export const ensureUniqueWindowSessionId=async()=> 'fixture';export const getUserContext=()=>({user_id:'#V#fixture',org_id:'#V#fixture_org'});export const getJson=async(url)=>{if(url.startsWith('/api/tasks/?'))return {tasks:[{...task,deployments:undefined}]};if(url==='/api/tasks/%23V%23task_fixture')return task;return {};};export const postJson=async()=>({});export const deleteJson=async()=>({});export const patchJson=async(url,body)=>{window.lastProductPatch={url,body};task.current_work_product={status:'ready',kind:'deployment',concept_id:body.current_work_product_concept_id,deployment:task.deployments[1]};return task;};`,
        '/js/tabNavigation.js': 'export const activateTab=()=>{};',
        '/js/utils/toast.js': 'export const showToast=()=>{};',
        '/js/markdownUtils.js': 'export const renderMarkdownViaServer=async()=>"";',
        '/js/utils/nameSelection.js': 'export const selectBestNameForContext=()=>null;export const selectShortestNameForContext=()=>null;',
        '/js/components/taskRunActivity.js': 'export const openTaskRunActivity=()=>{};',
    };
    const server = http.createServer((req,res) => {
        if(req.url==='/') {res.setHeader('Content-Type','text/html');res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/styles.css"></head><body><div id="globalTasksContainer"></div><script type="module">import {showGlobalTasks} from "/js/components/taskPanel.js";await showGlobalTasks();</script></body></html>');}
        else if(modules[req.url]) {res.setHeader('Content-Type','text/javascript');res.end(modules[req.url]);}
        else if(['/styles.css','/js/components/taskPanel.js'].includes(req.url)) {res.setHeader('Content-Type',req.url.endsWith('.css')?'text/css':'text/javascript');res.end(fs.readFileSync(path.join(root,req.url)));}
        else {res.statusCode=404;res.end();}
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    let browser;
    try {
        browser=await chromium.launch({headless:true,args:['--no-sandbox']});
        const page=await browser.newPage();
        const errors=[];page.on('pageerror',error=>errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.locator('.task-item').click();
        const inspector=page.locator('#globalTaskInspector');
        await inspector.locator('.task-deployment').first().waitFor();
        for(const width of [1440,390]) {
            await page.setViewportSize({width,height:1000});
            assert.equal(await inspector.locator('.task-deployment').count(),3);
            assert.equal(await inspector.locator('.task-select-deployment-btn').count(),1);
            await inspector.locator('summary').first().click();
            assert.match(await inspector.innerText(),/not a live deployment verification/);
            await inspector.locator('.task-select-deployment-btn').click();
            await page.waitForFunction(()=>window.lastProductPatch?.body.current_work_product_concept_id==='#V#deployment_1');
            assert.match(await inspector.innerText(),/Deployment: verified \(production\)/);
            assert.equal(await inspector.locator('.task-deployment').evaluateAll(nodes=>nodes.every(el=>el.scrollWidth<=el.clientWidth+1)),true);
            await page.screenshot({path:path.join(output,`deployments-${width}.png`),fullPage:true});
        }
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({passed:true,widths:[1440,390],checks:['three deployment attempts','unverified and rollback states','receipt history','verified product selection','no card overflow','no JavaScript errors'],scope:'candidate module and CSS, isolated API fixture; no live ingestion'},null,2));
        console.log('Deployment evidence browser fixture passed');
    } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
