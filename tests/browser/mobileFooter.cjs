// Read-only authenticated layout acceptance; organisation changes affect only
// the fixture session. Never sends drafts or invokes models/workflows.
// PLAYWRIGHT_BROWSERS_PATH=... node tests/browser/mobileFooter.cjs CANDIDATE BASELINE SHA DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const [candidate, baseline, sha, evidence] = process.argv.slice(2);
for (const url of [candidate, baseline]) assert(['localhost', '127.0.0.1'].includes(new URL(url).hostname));
fs.mkdirSync(evidence, { recursive: true });
const orgId = '#V#university_of_auckland_strong_ai_lab';
const metrics = [];
async function login(browser, base, mobile) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, isMobile: mobile, hasTouch: mobile });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    await page.route('**/von/generate', route => route.abort());
    const health = await (await page.request.get(`${base}/health`)).json();
    assert.equal(health.agent_test_instance, true);
    if (base === candidate) assert.equal(health.version_details.git_commit, sha);
    await page.goto(`${base}/von/`);
    await page.locator('#vonBrowserTestLoginButton').click();
    await expect(page.locator('#promptInput')).toBeVisible();
    await page.waitForTimeout(4000);
    const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
    assert.equal(auth.authenticated, true);
    assert.equal(auth.auth_provider, 'browser_test_fixture');
    return page;
}
async function switchScope(page, id) {
    await expect(async () => {
        const option = page.locator(`.footer-org-option[data-organisation-concept-id="${id}"]`);
        if (!await option.isVisible()) await page.locator('.footer-org-menu-trigger').click();
        if (!await option.isDisabled()) await option.click({ timeout: 2000 });
        else await page.locator('.footer-org-menu-trigger').click();
    }).toPass({ timeout: 15000 });
    await expect(page.locator('.footer-org-current-button')).toHaveAttribute('data-concept-id', id);
    await page.waitForTimeout(2500);
    const scope = await page.evaluate(async () => (await fetch('/von/api/session/context')).json());
    assert.equal(scope.organisation_id || '', id);
}
async function measure(page, name) {
    await page.waitForTimeout(250);
    const result = await page.evaluate(() => {
        const rect = selector => {
            const node = document.querySelector(selector);
            const r = node?.getBoundingClientRect();
            return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null;
        };
        const footer = document.querySelector('.footer-container');
        return { header: rect('#globalHeader'), tabs: rect('.tab-container'), footer: rect('.footer-container'),
            composer: rect(document.querySelector('#messageInput') ? '.message-compose-area' : '.chat-composer'),
            footerPosition: getComputedStyle(footer).position, width: innerWidth,
            overflow: document.documentElement.scrollWidth - innerWidth,
            clearance: getComputedStyle(document.documentElement).getPropertyValue('--von-fixed-footer-clearance') };
    });
    assert(result.overflow <= 1, `${name}: document overflow`);
    metrics.push({ name, ...result });
    fs.writeFileSync(path.join(evidence, 'measurements.json'), JSON.stringify(metrics, null, 2));
    await page.screenshot({ path: path.join(evidence, `${name}.png`), animations: 'disabled' });
    return result;
}
(async () => {
    const browser = await chromium.launch();
    try {
        const page = await login(browser, candidate, true);
        await switchScope(page, orgId);
        await page.locator('.chat-session-tab').filter({ hasText: 'MIT Contacts and Settings' }).click();
        await expect(page.locator('#promptInput')).toBeVisible();
        await page.waitForTimeout(2500);
        const draft = 'Unsent mobile footer acceptance draft';
        for (const kind of ['normal', 'agent']) {
            if (kind === 'agent') await page.locator('.message-conversation-row').filter({ hasText: 'Codex DGX' }).first().click();
            const input = page.locator(kind === 'normal' ? '#promptInput' : '#messageInput');
            if (kind === 'agent') {
                await expect(input).toHaveAttribute('placeholder', /Reply to Codex DGX/);
                await page.waitForTimeout(1500);
            }
            await input.fill(draft);
            for (const [name, width, height] of [['small', 360, 640], ['pixel', 412, 915], ['keyboard', 412, 430], ['foldable', 840, 900]]) {
                await page.setViewportSize({ width, height });
                await expect(input).toHaveValue(draft);
                const m = await measure(page, `${kind}-${name}`);
                assert.equal(m.clearance, '0px');
                assert(m.composer.y >= m.header.height + m.tabs.height, `${kind}-${name}: composer overlaps header`);
                assert(m.composer.y + m.composer.height <= height, `${kind}-${name}: composer clipped`);
                await page.locator(kind === 'normal' ? '#sendButton' : '#sendMessageBtn').click({ trial: true });
                await expect(page.locator('[data-tab="workflowStudioTab"]')).toBeHidden();
                await page.locator('.mobile-footer-details > summary').click();
                await expect(page.locator('#modelInfoFooter')).toBeVisible();
                if (kind === 'normal') {
                    const cost = page.locator('.conversation-runtime-cost-button[data-cost-scope="conversation"]');
                    await expect(cost).toBeVisible();
                    await cost.click();
                    await expect(cost).toHaveAttribute('aria-expanded', 'true');
                    await cost.click();
                }
                await page.screenshot({ path: path.join(evidence, `${kind}-${name}-info.png`) });
                await page.locator('.mobile-footer-details > summary').focus();
                await page.keyboard.press('Escape');
                await expect(page.locator('#modelInfoFooter')).toBeHidden();
                await expect(input).toHaveValue(draft);
            }
        }
        await page.setViewportSize({ width: 360, height: 640 });
        await switchScope(page, '');
        await switchScope(page, orgId);
        // Exercise the production asynchronous render entry point, without generation.
        await page.evaluate(async () => {
            const { setModelInfoFooterText } = await import('/static/js/domUtils.js');
            await setModelInfoFooterText();
        });
        await expect(page.locator('.mobile-shell-controls .footer-org-switcher')).toHaveCount(1);
        await expect(page.locator('.footer-container')).toBeHidden();
        await measure(page, 'after-refresh');
        await page.locator('.mobile-footer-details > summary').click();
        await page.locator('.conversation-model-controls button').click();
        await page.locator('.mobile-footer-details > summary').click();
        await expect(page.frameLocator('#settingsFrame').locator('#premium-model-settings')).toBeVisible();
        await expect(page.frameLocator('#settingsFrame').locator('#openaiReasoningEffortSelect')).toBeVisible();
        await page.screenshot({ path: path.join(evidence, 'model-settings.png') });
        const desktop = await login(browser, candidate, false);
        const control = await login(browser, baseline, false);
        for (const p of [desktop, control]) {
            await switchScope(p, orgId);
            await p.locator('.chat-session-tab').filter({ hasText: 'MIT Contacts and Settings' }).click();
            await p.waitForTimeout(2500);
        }
        const before = await measure(control, 'desktop-baseline');
        await desktop.locator('#promptInput').fill(draft);
        await desktop.setViewportSize({ width: 360, height: 640 });
        await desktop.setViewportSize({ width: 1440, height: 900 });
        await expect(desktop.locator('#promptInput')).toHaveValue(draft);
        await expect(desktop.locator('.mobile-shell-controls')).toHaveCount(0);
        const after = await measure(desktop, 'desktop-candidate');
        for (const key of ['header', 'tabs', 'footer', 'composer']) {
            for (const dimension of ['x', 'y', 'width', 'height']) assert(Math.abs(before[key][dimension] - after[key][dimension]) <= 1, `desktop ${key}.${dimension}`);
        }
        for (const [name, width, height] of [['small', 360, 640], ['pixel', 412, 915]]) {
            await control.setViewportSize({ width, height });
            await measure(control, `baseline-${name}`);
        }
        await desktop.locator('.footer-org-menu-trigger').click();
        await expect(desktop.locator('.footer-org-options')).toBeVisible();
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ sha, metrics, noSendsOrModelCalls: true, simulationLimits: 'Chromium viewport/touch simulation; no physical keyboard or public OAuth claim' }, null, 2));
        console.log(JSON.stringify({ sha, profiles: metrics.length, passed: true }));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
