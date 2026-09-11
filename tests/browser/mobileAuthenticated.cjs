// Local AgentTest server only. Sends one labelled message to the dedicated
// Workflow Reviewer fixture; blocks generation so no paid model is invoked.
// Usage: PLAYWRIGHT_BROWSERS_PATH=... node tests/browser/mobileAuthenticated.cjs URL SHA DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const [base, commit, evidence] = process.argv.slice(2);
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
fs.mkdirSync(evidence, { recursive: true });
(async () => {
    const browser = await chromium.launch();
    try {
        const page = await browser.newPage({ viewport: { width: 412, height: 915 }, isMobile: true, hasTouch: true });
        page.setDefaultTimeout(20000);
        await page.route('**/von/generate', route => route.abort());
        const health = await (await page.request.get(`${base}/health`)).json();
        assert.equal(health.version_details.git_commit, commit);
        await page.goto(`${base}/von/`);
        await page.locator('#vonBrowserTestLoginButton').click();
        await expect(page.locator('#promptInput')).toBeVisible();
        const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
        assert.equal(auth.authenticated, true);
        assert.equal(auth.auth_provider, 'browser_test_fixture');
        await page.locator('.footer-org-menu-trigger').click();
        await page.locator('.footer-org-option').filter({ hasText: 'University' }).click();
        const reviewer = () => page.locator('.message-conversation-row').filter({ hasText: 'Workflow Reviewer' });
        await reviewer().click();
        const draft = `Mobile layout acceptance ${Date.now()}: dedicated fixture message; no action requested.`;
        const input = page.locator('#messageInput');
        await input.fill(draft);
        for (const [name, width, height] of [
            ['pixel-8', 412, 915], ['keyboard', 412, 430], ['folded', 360, 740],
            ['tablet', 768, 1024], ['unfolded', 840, 900], ['desktop', 1440, 900]
        ]) {
            await page.setViewportSize({ width, height });
            await expect(input).toHaveValue(draft);
            await page.locator('#sendMessageBtn').click({ trial: true });
            const size = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
            assert(size.document <= size.viewport + 1, `${name}: no global overflow`);
            await page.screenshot({ path: path.join(evidence, `${name}.png`), animations: 'disabled' });
        }
        await page.setViewportSize({ width: 412, height: 915 });
        const sent = page.waitForResponse(response => response.url().endsWith('/api/messages/') && response.request().method() === 'POST');
        await page.locator('#sendMessageBtn').click();
        assert([200, 201].includes((await sent).status()));
        await expect(input).toHaveValue('');
        await expect(page.locator('#messageViewContent')).toContainText(draft);
        await page.reload();
        await reviewer().click();
        await expect(page.locator('#messageViewContent')).toContainText(draft);
        await page.locator('.mobile-footer-details > summary').click();
        await expect(page.locator('#serverUptimeFooter')).toBeVisible();
        await page.locator('.mobile-footer-details > summary').click();
        await page.locator('.footer-org-menu-trigger').click();
        await expect(page.locator('.footer-org-option').filter({ hasText: 'Personal' })).toBeVisible();
        await page.locator('.footer-org-menu-trigger').click();
        await page.getByRole('button', { name: 'Settings', exact: true }).click();
        await page.screenshot({ path: path.join(evidence, 'settings.png'), animations: 'disabled' });
        const result = { commit, authenticated: true, authProvider: auth.auth_provider, fixtureRecipient: 'Workflow Reviewer', sentAndReadBack: true, profiles: 6, generationBlocked: true };
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2));
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
