// Candidate-source fixture with mocked APIs; no backend, credentials or model calls.
// Optional native zoom: DISPLAY=<isolated display> XDOTOOL=<path> ... node this-file.
const { chromium, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { createHash } = require('node:crypto');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const template = fs.readFileSync(path.join(root, 'templates/von_interface.html'), 'utf8');
const footer = template.slice(template.indexOf('  <!-- Footer for Model Info -->'), template.indexOf('  <!-- RAG Status Modal -->')).replace(/{%.*?%}/g, '');
const output = process.argv[2] || '.run/footer-model-visibility';
const baseline = process.argv.includes('--baseline');
const native = Boolean(process.env.DISPLAY && process.env.XDOTOOL);
const html = `<link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css">
<button id="chat">Chat</button><button id="exchange">Messages</button>
<button class="tab-button" data-tab="settingsTab" onclick="window.settingsOpened=true">Settings</button>
<div id="conversationWorkspace"><div id="messagesContainer"></div></div>${footer}
<script type="module">
import { setModelInfoFooterText } from '/static/js/domUtils.js';
import { selectMessageConversation, showChatConversation } from '/static/js/components/conversationCatalogue.js';
import { mountFooterPreferences } from '/static/js/utils/footerPreferences.js';
localStorage.setItem('von_current_user', JSON.stringify({name:'Footer Test User',concept_id:'#V#footer_test'}));
window.refreshFooter = setModelInfoFooterText;
window.openExchange = () => selectMessageConversation({session_id:'fixture',viewer_id:'#V#footer_test',participant_ids:['#V#footer_test','#V#peer'],other_participant_ids:['#V#peer'],session_name:'Fixture peer'});
document.querySelector('#exchange').onclick = window.openExchange;
document.querySelector('#chat').onclick = showChatConversation;
mountFooterPreferences();
document.documentElement.setAttribute('data-footer-database','true');
await setModelInfoFooterText();
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
    fs.mkdirSync(output, { recursive: true });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless:!native,args:native ? ['--window-size=1600,1000','--disable-gpu'] : []});
    const page = await browser.newPage({viewport:native ? null : {width:1600,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let missing = false;
    let releaseProbe;
    let pendingProbe;
    let probeWaiting = false;
    await page.route('**/api/**', async route => {
        const url = route.request().url();
        let body = {};
        if (/\/settings\/?$/.test(new URL(url).pathname)) body = missing ? {} : {resolved_llm:{model:'gpt-6-astra',provider:'openai'}};
        if (url.includes('/llm/info')) {
            if (pendingProbe) { probeWaiting = true; await pendingProbe; probeWaiting = false; }
            body = missing ? {} : {model:'gpt-6-astra',provider:'openai',status:'ready'};
        }
        if (url.includes('/auth/status')) body = {authenticated:true,email:'footer@example.test'};
        if (url.includes('capability-index/status')) body = {ready:true,status:'ready'};
        if (url.includes('my_organisations')) body = {organisations:[{concept_id:'#V#footer_org',name:'Fixture Organisation',role:'member'}]};
        if (url.includes('/messages/')) body = {success:true,messages:[],threads:[],conversations:[]};
        return route.fulfill({json:body});
    });
    const evidence = [];
    const model = page.locator('.conversation-model-controls');
    async function check(name, hidden = false) {
        await expect(model)[hidden ? 'toBeHidden' : 'toBeVisible']();
        await expect(page.locator('.footer-user-segment')).toBeVisible();
        await expect(page.locator('.footer-org-switcher')).toBeVisible();
        await expect(page.locator('.db-conn-badge')).toBeVisible();
        evidence.push(await page.evaluate(name => ({name,exchange:document.body.classList.contains('viewing-message-exchange'),modelDisplay:getComputedStyle(document.querySelector('.conversation-model-controls')).display,model:document.querySelector('.conversation-model-controls').textContent,dpr:devicePixelRatio,innerWidth,outerWidth}), name));
    }
    try {
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await page.waitForFunction(() => window.fixtureReady);
        await check('initial');
        await expect(model).toContainText('gpt-6-astra');
        await page.evaluate(() => window.openExchange());
        await check('message exchange', baseline);
        await page.screenshot({path:path.join(output,'message-exchange.png')});
        await page.evaluate(() => window.refreshFooter());
        await check('refresh in message exchange', baseline);
        await page.locator('#chat').click();
        await check('return to chat');
        pendingProbe = new Promise(resolve => { releaseProbe = resolve; });
        await page.evaluate(() => { window.footerPending = window.refreshFooter(); });
        await expect.poll(() => probeWaiting).toBe(true);
        await check('pending model status refresh');
        releaseProbe(); pendingProbe = null;
        await page.evaluate(() => window.footerPending);
        missing = true;
        await page.reload();
        await page.waitForFunction(() => window.fixtureReady);
        await check('reload without model metadata');
        await expect(model).toContainText('Not Set');
        await model.locator('button').click();
        expect(await page.evaluate(() => window.settingsOpened)).toBe(true);
        missing = false;
        await page.reload();
        await page.waitForFunction(() => window.fixtureReady);
        await page.evaluate(() => window.openExchange());
        if (native) {
            const key = (...args) => execFileSync(process.env.XDOTOOL,args,{encoding:'utf8'});
            await page.bringToFront();
            const windows = key('search','--onlyvisible','--class','Chromium').trim().split('\n');
            expect(windows).toHaveLength(1);
            key('windowfocus','--sync',windows[0]);
            for (const [zoom, presses] of [[1,0],[1.25,2],[1.5,1],[2,2],[1,-1]]) {
                if (presses <= 0) key('key','ctrl+0');
                else for (let n=0;n<presses;n++) key('key','ctrl+equal');
                await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(zoom);
                await check(`native zoom ${zoom}`, baseline);
            }
        }
        await page.locator('#chat').click();
        await page.setViewportSize({width:390,height:844});
        await check('narrow chat');
        await page.evaluate(() => document.documentElement.setAttribute('data-footer-model','false'));
        await check('explicit hidden preference', true);
        await page.evaluate(() => document.documentElement.setAttribute('data-footer-model','true'));
        await check('restored preference');
        expect(errors).toEqual([]);
        fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify({sourceCommit:execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),stylesheetSha256:createHash('sha256').update(fs.readFileSync(path.join(root,'static/css/participantProfile.css'))).digest('hex'),browser:browser.version(),baseline,native,fixture:'actual footer renderer, CSS and message/chat route functions; mocked API responses',evidence,errors},null,2));
        console.log(JSON.stringify(evidence));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
