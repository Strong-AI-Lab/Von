/** Tier 1 rendering fixture: real task panel/CSS, synthetic read-only API pages. */
const { chromium, expect } = require('@playwright/test');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve('src/frontend/web/von_interface/static');
const output = path.resolve(process.argv[2] || '.run/task-board-pagination');
const baseline = process.argv.find(arg => arg.startsWith('--baseline='))?.slice('--baseline='.length);
const task = i => ({ task_concept_id: `#V#fixture_task_${i}`, title: `Research task ${i}`, description: 'Review the research evidence and record the outcome.', status: 'pending', priority: 'medium' });
const requests = [];
const stubs = {
    '/js/apiService.js': `export const WINDOW_SESSION_HEADER='X-Von-Window-Session'; export const ensureUniqueWindowSessionId=async()=> 'fixture'; export const getUserContext=()=>({user_id:'#V#fixture',org_id:'#V#fixture_org'}); export const getJson=async url=>(await fetch(url)).json(); export const postJson=()=>{throw Error('Unexpected write')}; export const patchJson=postJson; export const deleteJson=postJson;`,
    '/js/tabNavigation.js': 'export const activateTab=()=>{};',
    '/js/utils/toast.js': 'export const showToast=()=>{};',
    '/js/markdownUtils.js': 'export const renderMarkdownViaServer=async()=>"";',
};
const server = http.createServer((req,res) => {
    const url = new URL(req.url,'http://localhost');
    if(url.pathname === '/') {
        res.setHeader('Content-Type','text/html');
        res.end('<!doctype html><html><head><link rel="stylesheet" href="/styles.css"></head><body><main id="globalTasksTab" class="tab-content active"><div id="globalTasksContainer" class="global-tasks-content"></div></main><script type="module">import {showGlobalTasks} from "/js/components/taskPanel.js"; await showGlobalTasks();</script></body></html>');
    } else if (stubs[url.pathname]) {
        res.setHeader('Content-Type','text/javascript');res.end(stubs[url.pathname]);
    } else if(url.pathname === '/api/tasks/') {
        const offset = Number(url.searchParams.get('offset')); requests.push(Object.fromEntries(url.searchParams));
        const data = {tasks: Array.from({length:50},(_,i)=>task(offset+i)),count:50,offset,has_more:offset===0};
        res.setHeader('Content-Type','application/json');
        setTimeout(()=>res.end(JSON.stringify(data)),offset ? 600 : 0);
    } else if(url.pathname.startsWith('/api/')) {
        res.setHeader('Content-Type','application/json');
        res.end(JSON.stringify(url.pathname === '/api/tasks/taxonomy' ? {task_types:[],task_sources:[],defaults:{}} : {}));
    } else {
        const file = path.join(root,url.pathname);
        if(!file.startsWith(root + '/') || !fs.existsSync(file)) {res.statusCode=404;res.end();return;}
        res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'text/javascript');
        let source=fs.readFileSync(file,'utf8');
        if(baseline && url.pathname === '/styles.css') source=require('node:child_process').execFileSync('git',['show',`${baseline}:src/frontend/web/von_interface/static/styles.css`],{encoding:'utf8'});
        res.end(source);
    }
});
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const browser=await chromium.launch();
    const measurements=[];
    try {
        const page=await browser.newPage();
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        for(const viewport of [{width:1920,height:1080},{width:1536,height:864},{width:1280,height:900}]) {
            await page.setViewportSize(viewport);
            await page.goto(`http://127.0.0.1:${server.address().port}/`);
            const more=page.getByRole('button',{name:'Load 50 more',exact:true});
            await expect(more).toBeVisible();
            await expect(page.locator('.task-inspector-card')).toBeVisible();
            const measure=async state=>{
                const result=await page.evaluate(()=>{
                    const board=document.querySelector('#globalTaskList');
                    const main=document.querySelector('.global-task-main').getBoundingClientRect();
                    const inspector=document.querySelector('#globalTaskInspector').getBoundingClientRect();
                    const btn=document.querySelector('[data-task-page-action]');
                    const style=btn && getComputedStyle(btn);
                    board.scrollTop=board.scrollHeight;
                    return {boardHeight:board.clientHeight,scrollTop:board.scrollTop,scrollHeight:board.scrollHeight,mainBottom:main.bottom,inspectorBottom:inspector.bottom,sideBySide:inspector.x>main.x+main.width-1,color:style?.color,background:style?.backgroundColor,overflow:document.documentElement.scrollWidth-innerWidth};
                });
                measurements.push({viewport,state,...result});
                if(!baseline) {
                    assert(result.scrollTop>0,'loaded cards remain scrollable');
                    assert(result.overflow<=1,'board does not overflow the document');
                    if(result.sideBySide && state==='open') assert(Math.abs(result.mainBottom-result.inspectorBottom)<2,'board and pagination fill inspector height');
                    if(result.color) assert.notEqual(result.color,result.background,'pagination label contrast');
                }
                await page.locator('#globalTaskPagination').scrollIntoViewIfNeeded();
                await page.screenshot({path:path.join(output,`${viewport.width}-${state}.png`)});
            };
            await measure('open');
            await more.click();
            await expect(page.getByRole('button',{name:'Loading…',exact:true})).toBeDisabled();
            await expect(page.locator('#globalTaskPagination')).toHaveText('No more tasks to load.');
            const ids=await page.locator('#globalTaskList .task-item[data-task-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.taskId));
            assert.equal(ids.length,100);assert.equal(new Set(ids).size,100);
            for(let i=0;i<100;i++) assert(ids.includes(task(i).task_concept_id));
            // The current UI always selects an inspector; hide it only to exercise
            // the requested closed-pane layout, without inventing a product control.
            await page.locator('#globalTaskInspector').evaluate(el=>el.hidden=true);
            await measure('closed-fixture');
        }
        assert.deepEqual(errors,[]);
        assert.equal(requests.length,6);requests.forEach(r=>assert.equal(r.limit,'50'));
        fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({scope:'isolated real-module browser fixture; no live authentication or deployment',baseline,requests,measurements,passed:true},null,2));
        console.log(JSON.stringify(measurements,null,2));
    } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
