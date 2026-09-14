// Operator-prepared disposable AgentTest only. Actual uploads/originals and PWA
// shell; generation is intercepted to preserve the fixture's no-model boundary.
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/tmp/von-playwright';
const { chromium, expect } = require('@playwright/test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const [url, revision, actor, output = '.run/image-picker-authenticated'] = process.argv.slice(2);
assert(url && revision && actor, 'Supply fixture URL, exact revision and synthetic actor');
assert(['localhost', '127.0.0.1'].includes(new URL(url).hostname));
const png = fs.readFileSync(path.join(__dirname, 'fixtures/attachment.png'));
fs.mkdirSync(output, { recursive: true });
const report = { revision, actor, url, generation: 'intercepted; no model or persisted turn claim', profiles: [] };
const save = () => fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(report, null, 2));

(async () => {
    const browser = await chromium.launch({ headless: true });
    try {
        report.browser = browser.version();
        for (const width of [360, 412, 1440]) {
            const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: width < 800, hasTouch: width < 800 });
            const page = await context.newPage();
            page.setDefaultTimeout(20000);
            const result = { width, requests: [], uploads: [] };
            report.profiles.push(result);
            // Install before opening the page: this test must never invoke a model.
            await page.route('**/von/generate*', async route => {
                const body = route.request().postDataJSON();
                result.requests.push({ prompt: body.prompt, image_attachment_ids: body.image_attachment_ids,
                    session_id: body.session_id, window_session: !!route.request().headers()['x-von-window-session'] });
                await route.fulfill({ json: { response: 'Fixture request received.', success: true,
                    terminal_status: 'completed', session_id: body.session_id, llm_debug: { model: 'fixture-no-model' } } });
            });
            await page.goto(`${url}/von/`);
            const health = await (await context.request.get(`${url}/health`)).json();
            assert.equal(health.agent_test_instance, true);
            assert.equal(health.version_details.git_commit, revision);
            result.served_revision = health.version_details.git_commit;
            const login = page.waitForResponse(r => r.url().endsWith('/api/auth/browser-test-login') && r.request().method() === 'POST');
            await page.getByRole('button', { name: /Browser test login/i }).click();
            assert.equal((await login).status(), 200);
            await expect.poll(() => page.evaluate(async () => {
                const auth = await (await fetch('/von/api/auth/status')).json();
                return auth.authenticated && auth.user_concept_id;
            }).catch(() => null), { timeout: 20000 }).toBe(actor);
            const created = page.waitForResponse(r => r.url().endsWith('/api/session/create_chat_session') && r.request().method() === 'POST');
            await page.getByRole('button', { name: 'New conversation', exact: true }).click();
            assert.equal((await created).status(), 200);
            const input = page.locator('#promptInput');
            await expect(input).toBeEnabled();
            await input.fill('Describe this screenshot\nKeep my caption');
            // Verify the actual served PWA shell and controlling worker.
            result.pwa = await page.evaluate(async () => {
                const manifest = await (await fetch(document.querySelector('link[rel=manifest]').href)).json();
                await navigator.serviceWorker.register('/von/service-worker.js', { scope: '/von/', updateViaCache: 'none' });
                const registration = await navigator.serviceWorker.ready;
                return { display: manifest.display, start_url: manifest.start_url, scope: registration.scope,
                    script: registration.active.scriptURL, secure: isSecureContext };
            });
            await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
            assert.equal(result.pwa.display, 'standalone');
            assert.equal(result.pwa.secure, true);
            const attach = page.getByRole('button', { name: 'Attach image', exact: true });
            await expect(attach).toBeVisible();
            const box = await attach.boundingBox();
            assert(box.width >= 44 && box.height >= 44);
            async function upload(name, chooser = false) {
                const response = page.waitForResponse(r => r.url().endsWith('/api/images/upload') && r.request().method() === 'POST');
                const file = { name, mimeType: 'image/png', buffer: png };
                if (chooser) {
                    const selected = page.waitForEvent('filechooser');
                    await attach.click();
                    await (await selected).setFiles(file);
                } else await page.locator('#attachImageInput').setInputFiles(file);
                const uploaded = await response;
                assert.equal(uploaded.status(), 201);
                const descriptor = (await uploaded.json()).image_attachment;
                result.uploads.push(descriptor.concept_id);
                assert.equal(descriptor.sha256, crypto.createHash('sha256').update(png).digest('hex'));
                const original = await context.request.get(`${url}/von/api/images/${encodeURIComponent(descriptor.concept_id)}/original`);
                assert.equal(original.status(), 200);
                assert.deepEqual(await original.body(), png);
                await expect(page.locator('#conversationImageComposer img')).toBeVisible();
                await expect.poll(() => page.locator('#conversationImageComposer img').evaluate(img => img.naturalWidth)).toBeGreaterThan(0);
                return descriptor.concept_id;
            }
            const removed = await upload('phone.png', true);
            await page.getByRole('button', { name: 'Remove phone.png', exact: true }).click();
            await expect(page.locator('#conversationImageComposer img')).toHaveCount(0);
            const replacement = await upload('replacement.png');
            await page.locator('#attachImageInput').dispatchEvent('cancel');
            await expect(input).toHaveValue('Describe this screenshot\nKeep my caption');
            result.overflow = await page.locator('.chat-composer').evaluate(el => el.scrollWidth > el.clientWidth);
            assert.equal(result.overflow, false);
            await page.screenshot({ path: path.join(output, `${width}-preview.png`) });
            await page.locator('#sendButton').click();
            await expect.poll(() => result.requests.length).toBe(1);
            assert.deepEqual(result.requests[0].image_attachment_ids, [replacement]);
            assert(!result.requests[0].image_attachment_ids.includes(removed));
            assert.equal(result.requests[0].prompt, 'Describe this screenshot\nKeep my caption');
            assert(result.requests[0].window_session);
            await expect(page.locator('#conversationImageComposer')).toBeEmpty();
            await input.fill('Next text-only request');
            await page.locator('#sendButton').click();
            await expect.poll(() => result.requests.length).toBe(2);
            assert.equal(result.requests[1].image_attachment_ids, undefined);
            result.original_bytes_verified = true;
            result.request_binding_verified = true;
            // One mobile profile exercises recovery against actual retry upload.
            if (width === 360) {
                await input.fill('Preserve this failed-upload draft');
                await page.route('**/von/api/images/upload', r => r.fulfill({ status: 503, json: { error: 'Fixture upload failure' } }));
                await page.locator('#attachImageInput').setInputFiles({ name: 'retry.png', mimeType: 'image/png', buffer: png });
                const retry = page.getByRole('button', { name: 'Retry upload of retry.png', exact: true });
                await expect(retry).toBeVisible();
                await expect(input).toHaveValue('Preserve this failed-upload draft');
                await page.locator('#sendButton').click();
                assert.equal(result.requests.length, 2);
                await page.unroute('**/von/api/images/upload');
                const retried = page.waitForResponse(r => r.url().endsWith('/api/images/upload'));
                await retry.click();
                assert.equal((await retried).status(), 201);
                await expect(retry).toHaveCount(0);
                await page.getByRole('button', { name: 'Remove retry.png', exact: true }).click();
                result.upload_failure_recovery = true;
            }
            save();
            await context.close();
        }
        report.passed = true;
        save();
        console.log(JSON.stringify({ passed: true, revision, widths: report.profiles.map(p => p.width) }));
    } catch (error) { report.error = String(error); save(); throw error; }
    finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
