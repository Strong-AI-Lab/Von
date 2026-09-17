// Candidate-source layout acceptance. API fixtures make latency states repeatable;
// no authentication, database, model request or live service is used.
const { chromium, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { execFileSync } = require('node:child_process');
const nativeZoom = process.env.NATIVE_FOOTER_ZOOM === '1';
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const template = fs.readFileSync(path.join(root, 'templates/von_interface.html'), 'utf8');
const footer = template.slice(template.indexOf('  <!-- Footer for Model Info -->'), template.indexOf('  <!-- RAG Status Modal -->')).replace(/{%.*?%}/g, '');
const html = `<link rel="stylesheet" href="/static/styles.css">${footer}<script type="module">
import {setModelInfoFooterText, setFooterServerReachability} from '/static/js/domUtils.js';
localStorage.setItem('von_current_user', JSON.stringify({name:'footer.researcher.with.long.name@example.test', concept_id:'#V#footer_test'}));
localStorage.setItem('von:footerVisibility', JSON.stringify({database:true}));
window.dispatchEvent(new Event('von-footer-preferences-changed'));
window.setReachability = setFooterServerReachability;
setFooterServerReachability(true);
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
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({headless:!nativeZoom, args:['--window-size=1600,1000']});
    const page = await browser.newPage({viewport:nativeZoom ? null : {width:1600,height:800}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let delay = 5, db = 0.8;
    await page.route('**/api/**', async route => {
        const url = new URL(route.request().url()).pathname;
        let body = {};
        if (/\/settings\/?$/.test(url)) body = {resolved_llm:{model:'gpt-6-astra',provider:'openai'}};
        if (url.includes('/llm/info')) body = {model:'gpt-6-astra',provider:'openai',status:'ready',details:{}};
        if (url.includes('/auth/status')) body = {authenticated:true,email:'footer.researcher.with.long.name@example.test'};
        if (url.includes('capability-index/status')) body = {ready:true,status:'ready'};
        if (url.includes('my_organisations')) body = {organisations:[{concept_id:'#V#footer_org',name:'Footer Fixture Organisation',role:'member'}]};
        if (url.includes('/db/info')) {
            await new Promise(resolve => setTimeout(resolve, delay));
            body = {classification:'local',ping_ok:true,mongo_ping_latency_ms:db};
        }
        await route.fulfill({json:body});
    });
    const output = process.argv[2] || '.run/footer-compact-cartouches';
    fs.mkdirSync(output,{recursive:true});
    const evidence = [];
    const clipped = locator => locator.evaluate(el => getComputedStyle(el).clipPath === 'inset(50%)');
    try {
        for (const scenario of [
            {name:'normal',delay:5,db:0.8,rttQuiet:true,dbQuiet:true},
            {name:'slow-rtt',delay:304,db:0.8,rttQuiet:false,dbQuiet:true},
            {name:'slow-db',delay:5,db:501,rttQuiet:true,dbQuiet:false},
            {name:'partial',delay:5,db:null,rttQuiet:true,dbQuiet:false},
        ]) {
            delay = scenario.delay; db = scenario.db;
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.waitForFunction(() => window.fixtureReady);
            await expect(page.locator('.conversation-model-controls')).toContainText('gpt-6-astra');
            const badge = page.locator('.db-conn-badge');
            const rtt = badge.locator('.server-rtt-latency');
            const ping = badge.locator('.db-latency');
            await expect(badge).not.toHaveClass(/loading/);
            await page.mouse.move(0,0);
            await expect.poll(() => clipped(rtt), {message:scenario.name}).toBe(scenario.rttQuiet);
            await expect.poll(() => clipped(ping)).toBe(scenario.dbQuiet);
            const compact = (await badge.boundingBox()).width;
            await badge.hover();
            expect(await clipped(rtt)).toBe(false);
            expect(await clipped(ping)).toBe(false);
            const expanded = (await badge.boundingBox()).width;
            expect(expanded).toBeGreaterThan(compact);
            await page.mouse.move(0,0);
            // Reach the informational badge through actual sequential keyboard navigation.
            for (let i=0; i<20 && !await badge.evaluate(el => el === document.activeElement); i++) await page.keyboard.press('Tab');
            expect(await badge.evaluate(el => el === document.activeElement)).toBe(true);
            expect(await clipped(ping)).toBe(false);
            await page.screenshot({path:path.join(output,`${scenario.name}-focus.png`)});
            evidence.push({scenario:scenario.name,compact,expanded,rtt:await rtt.textContent(),db:await ping.textContent()});
            await page.evaluate(() => window.setReachability(false));
            expect(await clipped(rtt)).toBe(false);
            expect(await clipped(ping)).toBe(false);
            await expect(ping).toHaveText('DB unknown');
        }
        if (nativeZoom) {
            if (!process.env.DISPLAY || !process.env.XDOTOOL) throw new Error('Native zoom requires an isolated display and xdotool');
            const key = (...args) => execFileSync(process.env.XDOTOOL,args,{encoding:'utf8'});
            await page.bringToFront();
            const windows = key('search','--onlyvisible','--class','Chromium').trim().split('\n');
            expect(windows).toHaveLength(1);
            key('windowfocus','--sync',windows[0]);
            key('key','ctrl+0');
            const cdp = await page.context().newCDPSession(page);
            for (const [steps,ratio] of [[0,1],[2,1.25],[1,1.5],[2,2]]) {
                for (let i=0;i<steps;i++) key('key','ctrl+equal');
                await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(ratio);
                await page.locator('.db-conn-badge').focus();
                expect(await clipped(page.locator('.db-latency'))).toBe(false);
                const measurement = await page.locator('.footer-container').evaluate(el => ({zoom:devicePixelRatio,width:innerWidth,height:el.getBoundingClientRect().height,scrollWidth:el.scrollWidth}));
                expect(measurement.height).toBeLessThan(80);
                evidence.push(measurement);
                const shot = await cdp.send('Page.captureScreenshot',{format:'png',fromSurface:false});
                fs.writeFileSync(path.join(output,`zoom-${ratio}.png`),Buffer.from(shot.data,'base64'));
            }
            key('key','ctrl+0');
            await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(1);
        }
        delay = 1500; db = 0.8;
        await page.goto(`http://127.0.0.1:${server.address().port}`);
        await expect(page.locator('.db-conn-badge.loading')).toBeVisible();
        expect(await clipped(page.locator('.db-latency'))).toBe(false);
        await page.waitForFunction(() => window.fixtureReady);
        for (const width of [1600,800,493,320]) {
            await page.setViewportSize({width,height:800});
            for (const selector of ['.conversation-model-controls','.footer-user-segment','.footer-org-switcher']) {
                const segment = page.locator(selector).first();
                await segment.locator(selector === '.footer-org-switcher' ? 'summary' : 'button:not(:disabled)').first().focus();
                expect(await clipped(segment.locator('.footer-label-inline').first()), `${width} ${selector}`).toBe(false);
                const box = await segment.boundingBox();
                expect(box.height).toBeLessThan(60);
            }
            await page.locator('.db-conn-badge').focus();
            const measure = await page.locator('.footer-container').evaluate(el => {
                const badge = el.querySelector('.db-conn-badge').getBoundingClientRect();
                const previous = el.querySelector('.footer-org-switcher').getBoundingClientRect();
                return {width:innerWidth,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,height:el.getBoundingClientRect().height,noOverlap:badge.x >= previous.right};
            });
            expect(measure.noOverlap).toBe(true);
            expect(measure.height).toBeLessThan(80);
            evidence.push(measure);
        }
        expect(errors).toEqual([]);
        fs.writeFileSync(path.join(output,'measurements.json'),JSON.stringify({fixture:'candidate source; mocked API reads',nativeZoom,evidence},null,2));
        console.log(JSON.stringify(evidence));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
