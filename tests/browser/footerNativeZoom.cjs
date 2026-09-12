// Local read-only fixture acceptance using real X keyboard browser zoom.
// DISPLAY must name an isolated test display; XDOTOOL and LD_LIBRARY_PATH may
// point to operator-prepared tools. No model generation or saved settings writes.
// node tests/browser/footerNativeZoom.cjs URL SHA EVIDENCE_DIR [baseline]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium, expect: baseExpect } = require('@playwright/test');
const expect = baseExpect.configure({ timeout: 25000 });
const [base, sha, output, mode] = process.argv.slice(2);
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
assert(process.env.DISPLAY, 'Use an isolated operator-prepared X display');
fs.mkdirSync(output, { recursive: true });
const key = (...args) => execFileSync(process.env.XDOTOOL || 'xdotool', args, { encoding: 'utf8' });
(async () => {
    const browser = await chromium.launch({ headless: false, args: ['--window-size=1600,1000', '--disable-gpu'] });
    const receipt = { browser: browser.version(), sha, base, mode: mode || 'candidate',
        method: 'Native X Ctrl+equal / Ctrl+0, fixed 1600x1000 outer window; viewport:null. Narrow case is viewport resizing only.', cases: [] };
    try {
        const context = await browser.newContext({ viewport: null });
        const page = await context.newPage();
        page.setDefaultTimeout(25000);
        await page.route('**/von/generate', route => route.abort());
        const health = await (await page.request.get(`${base}/health`)).json();
        assert.equal(health.agent_test_instance, true);
        assert.equal(health.version_details.git_commit, sha);
        receipt.health = health.version_details;
        await page.goto(`${base}/von/`);
        await page.locator('#vonBrowserTestLoginButton').click();
        await expect(page.locator('#promptInput')).toBeVisible();
        await expect(page.locator('.footer-info .footer-segment').first()).toBeVisible();
        await page.waitForTimeout(4000);
        const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
        assert.equal(auth.authenticated, true);
        assert.equal(auth.auth_provider, 'browser_test_fixture');
        receipt.actor = auth.user_concept_id || auth.user?.concept_id || auth.user?.user_concept_id;
        await page.locator('#promptInput').fill('Unsent footer zoom acceptance draft');
        await page.bringToFront();
        const windows = key('search', '--onlyvisible', '--class', 'Chromium').trim().split('\n');
        assert.equal(windows.length, 1, 'The isolated display must have exactly one Chromium window');
        key('windowfocus', '--sync', windows[0]);
        const cdp = await context.newCDPSession(page);
        async function capture(name) {
            // Playwright surface capture can produce blank images at native zoom.
            const shot = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: false });
            fs.writeFileSync(path.join(output, `${name}.png`), Buffer.from(shot.data, 'base64'));
        }
        async function check(name, ratio, narrow = false) {
            await expect.poll(() => page.evaluate(() => devicePixelRatio)).toBe(ratio);
            await page.waitForTimeout(800);
            await expect(page.locator('#promptInput')).toHaveValue('Unsent footer zoom acceptance draft');
            const mobile = await page.locator('.mobile-footer-details > summary').isVisible();
            if (mobile) await page.locator('.mobile-footer-details > summary').click();
            const m = await page.evaluate(() => {
                const rect = node => {
                    const r = node.getBoundingClientRect();
                    return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom };
                };
                const footer = document.querySelector('.footer-container');
                const language = document.querySelector('#languageIndicator');
                const range = document.createRange(); range.selectNodeContents(language);
                return { dpr: devicePixelRatio, innerWidth, innerHeight, outerWidth, outerHeight,
                    scrollWidth: document.documentElement.scrollWidth,
                    clearance: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--von-fixed-footer-clearance')),
                    footer: rect(footer), composer: rect(document.querySelector('.chat-composer')),
                    languageLines: range.getClientRects().length, language: language.textContent,
                    groups: [...footer.children].filter(e => e.getBoundingClientRect().height).map(e => ({ id: e.id, ...rect(e) })),
                    build: document.querySelector('#serverBuildValue').textContent };
            });
            receipt.cases.push({ name, mobile, ...m });
            await capture(name);
            if (mode !== 'baseline') {
                assert(m.scrollWidth <= m.innerWidth + 1, `${name}: document overflow`);
                assert.equal(m.languageLines, 1, `${name}: split language label`);
                for (const g of m.groups) {
                    assert(g.x >= m.footer.x - 1 && g.right <= m.footer.right + 1, `${name}: group clipped`);
                }
                if (!mobile) {
                    assert(m.clearance >= m.footer.height, `${name}: insufficient reserved height`);
                    assert(m.composer.bottom <= m.footer.y, `${name}: composer overlaps footer`);
                } else assert.equal(m.clearance, 0);
            }
            if (!narrow) assert.equal(m.outerWidth, 1600);
            // Open existing controls without changing a model or invoking it.
            const cost = page.locator('.conversation-runtime-cost-button[data-cost-scope="conversation"]');
            await cost.click(); await expect(cost).toHaveAttribute('aria-expanded', 'true'); await cost.click();
            if (mobile) {
                await page.locator('.mobile-footer-details > summary').focus(); await page.keyboard.press('Escape');
                await expect(page.locator('.footer-container')).toBeHidden();
            }
            await page.locator('.footer-org-menu-trigger').click();
            await expect(page.locator('.footer-org-options')).toBeVisible();
            await page.locator('.footer-org-menu-trigger').click();
            await page.locator('#sendButton').click({ trial: true });
            fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2));
        }
        for (const [name, keys, ratio] of [
            ['100', ['ctrl+0'], 1], ['125', ['ctrl+equal', 'ctrl+equal'], 1.25],
            ['150', ['ctrl+equal'], 1.5], ['200', ['ctrl+equal', 'ctrl+equal'], 2], ['reset100', ['ctrl+0'], 1]
        ]) {
            for (const k of keys) key('key', '--clearmodifiers', k);
            await check(name, ratio);
        }
        await page.setViewportSize({ width: 360, height: 640 });
        await check('narrow360', 1, true);
        await page.locator('.mobile-footer-details > summary').click();
        await page.locator('.conversation-model-controls button').click();
        await page.locator('.mobile-footer-details > summary').click();
        await expect(page.frameLocator('#settingsFrame').locator('#premium-model-settings')).toBeVisible();
        receipt.modelSettingsReachable = true;
        receipt.completed = true;
        fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2));
        console.log(`${receipt.mode}: ${receipt.cases.length} cases passed; evidence ${output}`);
    } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
