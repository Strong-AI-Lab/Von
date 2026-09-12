/** Isolated UI fixture; does not authenticate to Von or invoke a model. */
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
    const output = path.resolve(process.argv[2] || '.run/task-run-activity');
    fs.mkdirSync(output, { recursive: true });
    const moduleSource = fs.readFileSync(path.resolve('src/frontend/web/von_interface/static/js/components/taskRunActivity.js'), 'utf8');
    const run = { run_id: 'a'.repeat(32), started_at: '2026-09-12T12:00:00Z', updated_at: '2026-09-12T12:00:15Z', capture_status: 'running', event_count: 2, provenance: { execution_settings: { model: 'gpt-6-astra', reasoning_effort: 'high' } } };
    let terminal = false;
    let downloadHeader;
    const server = http.createServer((req, res) => {
        if (req.url === '/') {
            res.setHeader('Content-Type', 'text/html');
            res.end('<!doctype html><html><body><button id="open">Coding run activity</button><script type="module">import {openTaskRunActivity} from "/components/taskRunActivity.js";document.querySelector("#open").onclick=()=>openTaskRunActivity("#V#task");</script></body></html>');
        } else if (req.url === '/components/taskRunActivity.js') {
            res.setHeader('Content-Type', 'text/javascript'); res.end(moduleSource);
        } else if (req.url === '/apiService.js') {
            res.setHeader('Content-Type', 'text/javascript');
            res.end('export const WINDOW_SESSION_HEADER="X-Von-Window-Session"; export async function ensureUniqueWindowSessionId(){return "fixture-window";} export function getUserContext(){return {user_id:"fixture",org_id:"fixture-org"};} export async function getJson(url){const r=await fetch(url);if(!r.ok)throw Error();return r.json();}');
        } else if (req.url.endsWith('/download')) {
            downloadHeader = req.headers['x-von-window-session'];
            res.setHeader('Content-Type', 'application/zip'); res.end('fixture-download-bytes');
        } else if (req.url.startsWith('/api/tasks/')) {
            res.setHeader('Content-Type', 'application/json');
            if (!req.url.includes('/' + run.run_id)) res.end(JSON.stringify({ runs: [run], next_offset: null }));
            else res.end(JSON.stringify({run: {...run, ...(terminal ? {capture_status:'complete',archive_url:'/api/tasks/task/runs/run/download',manifest:{source_complete:true,reasoning_summaries:{availability:'not exposed in captured exec stream'}}} : {})}, records:[{text:'<script>window.archiveExecuted=true</script>\nCommand output\n'+'x'.repeat(5000),display_truncated:true}],next_cursor:123,has_more:false,projection:'Display is limited; full redacted output is archived.'}));
        } else { res.statusCode = 404; res.end(); }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let browser;
    try {
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage({ acceptDownloads: true });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        const url = `http://127.0.0.1:${server.address().port}/`;
        for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
            await page.setViewportSize(viewport);
            await page.goto(url);
            await page.locator('#open').click();
            await page.getByRole('button', {name: new RegExp(run.run_id)}).click();
            await page.getByRole('status').filter({hasText:'running'}).waitFor();
            assert.equal(await page.evaluate(() => window.archiveExecuted), undefined);
            const overflow = await page.locator('dialog').evaluate(el => el.scrollWidth > el.clientWidth + 2);
            assert.equal(overflow, false);
            await page.screenshot({path:path.join(output, `activity-${viewport.width}.png`)});
            terminal = true;
            await page.getByRole('button', {name:'Refresh this page'}).click();
            await page.getByRole('status').filter({hasText:'complete'}).waitFor();
            const downloadPromise = page.waitForEvent('download');
            await page.getByRole('button', {name:'Download archive'}).click();
            const download = await downloadPromise;
            await download.saveAs(path.join(output, `download-${viewport.width}.zip`));
            assert.equal(downloadHeader, 'fixture-window');
            await page.getByRole('button', {name:'Close',exact:true}).click();
            await page.locator('dialog').waitFor({state:'detached'});
            terminal = false;
        }
        await page.locator('#open').click();
        await page.locator('dialog').waitFor();
        await page.evaluate(() => document.dispatchEvent(new Event('orgSwitched')));
        await page.locator('dialog').waitFor({state:'detached'});
        assert.deepEqual(errors, []);
        fs.writeFileSync(path.join(output,'receipt.json'), JSON.stringify({scope:'isolated UI fixture, no live authentication or model calls',checks:['desktop/mobile no horizontal overflow','source text inert','running/final refresh','window-scoped download','dialog closes and clears on scope change'],passed:true},null,2));
        console.log('Task run activity browser fixture passed');
    } finally {
        if (browser) await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => {console.error(error);process.exitCode=1;});
