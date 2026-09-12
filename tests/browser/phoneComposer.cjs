// Authenticated localhost acceptance with synthetic transcript bodies and no sends.
// PLAYWRIGHT_BROWSERS_PATH=... node tests/browser/phoneComposer.cjs CANDIDATE BASELINE SHA DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const [candidate, baseline, sha, evidence] = process.argv.slice(2);
for (const base of [candidate, baseline]) assert(['localhost', '127.0.0.1'].includes(new URL(base).hostname));
fs.mkdirSync(evidence, { recursive: true });
const results = [];
const org = '#V#university_of_auckland_strong_ai_lab';
async function login(browser, base, touch) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, hasTouch: touch, isMobile: touch });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    page.submissions = [];
    await page.route('**/*', async route => {
        const req = route.request(), url = new URL(req.url());
        if (url.pathname === '/von/api/files/upload') return route.fulfill({ json: {
            success: true, uploaded: { concept_id: '#V#phone_composer_fixture_file' }
        } });
        if (url.pathname.includes('/chat_prompt_queue') && req.method() === 'POST') {
            page.submissions.push({ body: req.postDataJSON(), headers: req.headers() });
            await new Promise(resolve => { page.releaseSubmission = resolve; });
            return route.fulfill({ status: 503, json: { error: 'Isolated acceptance: no submission was persisted.' } });
        }
        if (url.pathname === '/von/history') return route.fulfill({ json: { authenticated: true, history: [], total_segments: 0, segments_returned: 0 } });
        if (url.pathname.endsWith('/api/messages/exchange') || url.pathname.includes('/api/messages/conversation/')) {
            return route.fulfill({ json: { messages: [], current_user_id: '#V#michael_witbrock' } });
        }
        if (url.pathname.includes('/api/messages/read/')) return route.fulfill({ json: { success: true } });
        if (url.pathname === '/von/generate' || url.pathname.includes('/api/speech/') && req.method() !== 'GET'
            || url.pathname.endsWith('/api/messages/') && req.method() !== 'GET'
            || url.pathname.includes('/prompt_queue') && req.method() !== 'GET') return route.abort();
        return route.continue();
    });
    const health = await (await page.request.get(`${base}/health`)).json();
    assert(health.agent_test_instance);
    assert.equal(health.version_details.git_commit, base === candidate ? sha : '9661e0fce7ecf552e69ed621559246f102f96179');
    if (base === baseline) assert.equal(health.version_details.git_dirty, false);
    await page.goto(`${base}/von/`);
    await page.locator('#vonBrowserTestLoginButton').click();
    await expect(page.locator('#promptInput')).toBeVisible();
    await page.waitForTimeout(2000);
    const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
    assert.equal(auth.authenticated, true);
    assert.equal(auth.auth_provider, 'browser_test_fixture');
    await page.locator('.footer-org-menu-trigger').click();
    const option = page.locator(`.footer-org-option[data-organisation-concept-id="${org}"]`);
    if (!await option.isDisabled()) await option.click();
    else await page.locator('.footer-org-menu-trigger').click();
    await expect(page.locator('.footer-org-current-button')).toHaveAttribute('data-concept-id', org);
    await page.waitForTimeout(2000);
    return page;
}
async function measure(page, name, message = false) {
    await page.waitForTimeout(200);
    const data = await page.evaluate(message => {
        const box = selector => {
            const r = document.querySelector(selector)?.getBoundingClientRect();
            return r && { x: r.x, y: r.y, width: r.width, height: r.height };
        };
        return { composer: box(message ? '#messageComposeArea' : '.chat-composer'),
            input: box(message ? '#messageInput' : '#promptInput'), header: box('#globalHeader'),
            width: innerWidth, height: innerHeight, overflow: document.documentElement.scrollWidth - innerWidth };
    }, message);
    assert(data.overflow <= 1, `${name}: document overflow`);
    assert(data.composer.y >= data.header.height, `${name}: header overlap`);
    assert(data.composer.y + data.composer.height <= data.height, `${name}: bottom clipping`);
    results.push({ name, ...data });
    fs.writeFileSync(path.join(evidence, 'measurements.json'), JSON.stringify(results, null, 2));
    await page.screenshot({ path: path.join(evidence, `${name}.png`) });
    return data;
}
(async () => {
    const browser = await chromium.launch();
    try {
        for (const base of [baseline, candidate]) {
            const label = base === baseline ? 'baseline' : 'candidate';
            const page = await login(browser, base, true);
            for (const [width, height] of [[360,640], [412,915], [412,360], [840,900]]) {
                await page.setViewportSize({ width, height });
                const input = page.locator('#promptInput');
                await input.fill('');
                const idle = await measure(page, `${label}-${width}-${height}-idle`);
                if (base === candidate) {
                    assert(idle.composer.height < 90, 'idle composer should recover substantial space');
                    await expect(page.locator('#sendButton')).toBeHidden();
                    await expect(page.locator('#dictateButton')).toBeHidden();
                    await expect(page.locator('.composer-add-icon')).toBeVisible();
                }
                await input.fill('A short unsent draft');
                await page.locator('#sendButton').click({ trial: true });
                await measure(page, `${label}-${width}-${height}-typed`);
                if (base === candidate) {
                    await input.press('Enter');
                    await expect(input).toHaveValue('A short unsent draft\n');
                    await input.fill('First line\nSecond line\nThird line');
                    await measure(page, `${label}-${width}-${height}-multiline`);
                    const summary = page.locator('.chat-composer-more-actions > summary');
                    await summary.click();
                    await expect(page.locator('#dictateButton')).toBeVisible();
                    await expect(page.locator('#voiceConversationButton')).toBeVisible();
                    await expect(page.locator('#uploadFileButton')).toBeVisible();
                    await expect(page.locator('#dictationEngineSelect')).toBeVisible();
                    await measure(page, `${label}-${width}-${height}-menu`);
                    await summary.focus(); await page.keyboard.press('Escape');
                    await expect(page.locator('.chat-composer-more-actions')).not.toHaveAttribute('open', '');
                    await expect(input).toHaveValue('First line\nSecond line\nThird line');
                }
            }
            if (base === candidate) {
                await page.setViewportSize({ width: 360, height: 640 });
                const input = page.locator('#promptInput');
                await input.fill('');
                await page.locator('#uploadFileInput').setInputFiles({ name: 'phone-fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic attachment') });
                await expect(page.locator('#pendingAttachmentStatus')).toContainText('phone-fixture.txt');
                await expect(page.locator('#sendButton')).toBeVisible();
                await expect(page.locator('#sendButton')).toBeEnabled();
                await measure(page, 'candidate-attachment-only');
                await input.fill('Unsent attachment draft');
                const summary = page.locator('.chat-composer-more-actions > summary');
                await summary.click(); await summary.press('Escape');
                await page.setViewportSize({ width: 840, height: 900 });
                await expect(input).toHaveValue('Unsent attachment draft');
                await expect(page.locator('#pendingAttachmentStatus')).toContainText('phone-fixture.txt');
                await input.dispatchEvent('keypress', { key: 'Enter', isComposing: true });
                assert.equal(page.submissions.length, 0, 'IME must not submit');
                const sessionId = await page.locator('.chat-session-tab.is-active[data-session-id]').getAttribute('data-session-id');
                await page.locator('#sendButton').click();
                await expect.poll(() => page.submissions.length).toBe(1);
                await page.evaluate(() => document.querySelector('#sendButton').click());
                assert.equal(page.submissions.length, 1, 'in-flight click must not duplicate submission');
                assert.equal(page.submissions[0].body.session_id, sessionId);
                if (page.submissions[0].body.dispatch_mode === 'server') {
                    assert(JSON.stringify(page.submissions[0].body).includes('#V#phone_composer_fixture_file'));
                } else {
                    // Browser dispatch first persists an admission record, before
                    // generation; the fixture rejects that record without effects.
                    assert(page.submissions[0].body.client_request_id);
                }
                assert(page.submissions[0].headers['x-von-window-session']);
                page.releaseSubmission();
                // Sending deliberately consumes the draft via the existing controller.
                // This fixture proves admission/duplicate prevention, not delivery.
                await page.waitForTimeout(300);
            }
            // A known coding-agent exchange, with transcript bodies intercepted above.
            await page.locator('.message-conversation-row').filter({ hasText: 'Codex DGX' }).first().click();
            const reply = page.locator('#messageInput');
            await expect(reply).toBeVisible();
            for (const [width,height] of [[360,640], [412,915], [412,360], [840,900]]) {
                await page.setViewportSize({ width,height });
                await reply.fill('');
                await measure(page, `${label}-message-${width}-${height}-idle`, true);
                if (base === candidate) await expect(page.locator('#sendMessageBtn')).toBeHidden();
                await reply.fill('First line\nSecond line');
                await page.locator('#sendMessageBtn').click({ trial: true });
                await measure(page, `${label}-message-${width}-${height}-typed`, true);
                if (base === candidate) {
                    await reply.press('Enter');
                    await expect(reply).toHaveValue('First line\nSecond line\n');
                }
            }
            await page.context().close();
        }
        const desktop = await login(browser, candidate, false);
        const control = await login(browser, baseline, false);
        await desktop.locator('#promptInput').fill('Unsent desktop draft');
        await control.locator('#promptInput').fill('Unsent desktop draft');
        await control.evaluate(() => window.scrollTo(0, 0));
        const before = await measure(control, 'desktop-baseline');
        await desktop.setViewportSize({ width: 360, height: 640 });
        await desktop.locator('.chat-composer-more-actions > summary').click();
        await desktop.setViewportSize({ width: 1440, height: 900 });
        await desktop.locator('.chat-composer-more-actions > summary').click();
        await expect(desktop.locator('#promptInput')).toHaveValue('Unsent desktop draft');
        await expect(desktop.locator('.chat-composer-primary-actions > #dictateButton')).toBeVisible();
        await expect(desktop.locator('.composer-more-icon')).toBeVisible();
        await desktop.evaluate(() => window.scrollTo(0, 0));
        const after = await measure(desktop, 'desktop-restored');
        for (const dimension of ['x','y','width','height']) assert(Math.abs(before.composer[dimension] - after.composer[dimension]) <= 1, `desktop composer ${dimension}`);
        console.log(JSON.stringify({ passed: true, measurements: results.length }));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
