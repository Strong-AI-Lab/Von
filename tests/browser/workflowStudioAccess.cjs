// Read-only acceptance on an operator-prepared localhost AgentTest server.
// No role changes, messages, workflow mutations or generation are performed.
// Run for each role with PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright:
// node tests/browser/workflowStudioAccess.cjs URL SHA admin|member EVIDENCE_DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const [base, commit, role, evidence] = process.argv.slice(2);
assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
assert(['admin', 'member'].includes(role));
assert(/^[0-9a-f]{40}$/.test(commit));
fs.mkdirSync(evidence, { recursive: true });
(async () => {
    const browser = await chromium.launch();
    try {
        for (const mobile of [false, true]) {
            const context = await browser.newContext({
                viewport: mobile ? { width: 412, height: 915 } : { width: 1440, height: 900 },
                isMobile: mobile, hasTouch: mobile
            });
            const page = await context.newPage();
            page.setDefaultTimeout(20000);
            await page.route('**/von/generate', route => route.abort());
            const health = await (await page.request.get(`${base}/health`)).json();
            assert.equal(health.version_details.git_commit, commit);
            for (const url of ['/von/workflow-studio', '/von/workflow-studio/content', '/api/workflow-studio/catalogue']) {
                assert.equal((await page.request.get(`${base}${url}`, { maxRedirects: 0 })).status(), 403);
            }
            const studioRequests = [];
            page.on('request', request => {
                if (/workflow-studio|workflowStudio(Page|Tab)\.js/.test(request.url())) studioRequests.push(request.url());
            });
            await page.goto(`${base}/von/#workflowStudioTab`);
            await page.locator('#vonBrowserTestLoginButton').click();
            await expect(page.locator('[data-tab="chatTab"]')).toBeVisible();
            const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
            assert.equal(auth.authenticated, true);
            assert.equal(auth.auth_provider, 'browser_test_fixture');
            assert.equal(auth.workflow_studio_access, role === 'admin');
            const studioButton = page.locator('[data-tab="workflowStudioTab"]');
            if (mobile || role === 'member') {
                await expect(studioButton).toBeHidden();
                await expect(page.locator('#chatTab')).toHaveClass(/active/);
                assert.equal(studioRequests.length, 0, 'blocked saved link must not lazy-load Studio');
                await page.evaluate(() => document.querySelector('[data-tab="workflowStudioTab"]').click());
                await expect(page.locator('#chatTab')).toHaveClass(/active/);
                assert.equal(studioRequests.length, 0);
            } else {
                await expect(studioButton).toBeVisible();
                await expect(page.locator('#workflowStudioTab')).toHaveClass(/active/);
                await expect(page.locator('#workflowStudioCatalogue')).toBeVisible();
                assert.equal((await page.request.get(`${base}/api/workflow-studio/catalogue`)).status(), 200);
            }
            if (role === 'member') {
                for (const url of ['/von/workflow-studio', '/von/workflow-studio/content', '/api/workflow-studio/catalogue']) {
                    assert.equal((await page.request.get(`${base}${url}`, { maxRedirects: 0 })).status(), 403);
                }
            }
            await page.screenshot({ path: path.join(evidence, `${role}-${mobile ? 'mobile' : 'desktop'}.png`) });
            // Use an existing conversation; edit only a local, unsent draft.
            await page.locator('[data-tab="chatTab"]').click();
            // Initial auth/scope refresh may rebuild and close the menu once.
            await expect(async () => {
                const organisation = page.locator('.footer-org-option[data-organisation-concept-id="#V#university_of_auckland_strong_ai_lab"]');
                if (!await organisation.isVisible()) await page.locator('.footer-org-menu-trigger').click();
                await organisation.click({ timeout: 2000 });
            }).toPass({ timeout: 15000 });
            await page.locator('.message-conversation-row').filter({
                hasText: role === 'admin' ? /Codex DGX|Codex VS Code/ : 'Workflow Reviewer'
            }).first().click();
            const draft = page.locator('#messageInput');
            await expect(draft).toHaveAttribute('placeholder', /Reply to/);
            await draft.fill('Unsent Studio access acceptance draft');
            if (!mobile && role === 'admin') await studioButton.click();
            await page.setViewportSize({ width: 412, height: 915 });
            await expect(studioButton).toBeHidden();
            await expect(page.locator('#chatTab')).toHaveClass(/active/);
            await expect(draft).toHaveValue('Unsent Studio access acceptance draft');
            await page.setViewportSize({ width: 840, height: 900 });
            if (mobile) await expect(studioButton).toBeHidden(); // coarse-pointer foldable
            else if (role === 'admin') await expect(studioButton).toBeVisible();
            await page.locator('[data-tab="globalTasksTab"]').click();
            await expect(page.locator('#globalTasksTab')).toHaveClass(/active/);
            await page.locator('[data-tab="settingsTab"]').click();
            await expect(page.frameLocator('#settingsFrame').locator('#preferredLanguageSelect')).toBeVisible();
            await context.close();
        }
        const result = { commit, role, desktopAndMobile: true, draftsPreserved: true, generationBlocked: true, noWorkflowOrMessageMutations: true };
        fs.writeFileSync(path.join(evidence, `${role}.json`), JSON.stringify(result, null, 2));
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
