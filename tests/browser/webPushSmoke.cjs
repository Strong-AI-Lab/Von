/* Transport attempt, not proof of installed-app or OS display acceptance.
 * PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/webPushSmoke.cjs
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
    const base = 'http://127.0.0.1:5077';
    const evidence = path.resolve('.run/web-push-smoke');
    fs.mkdirSync(evidence, { recursive: true });
    const headed = process.env.VON_PUSH_HEADED === '1';
    const browser = await chromium.launch({ headless: !headed });
    const context = await browser.newContext({ permissions: ['notifications'], viewport: { width: 390, height: 844 } });
    const page = await context.newPage();
    const report = { browser: browser.version(), os: process.platform,
        installed: false, mode: `${headed ? 'headed' : 'headless'} ordinary tab; injected permission`,
        push_method: 'PushManager.subscribe then encrypted pywebpush, if registration succeeds',
        device_display_verified: false };
    try {
        report.fixture = await (await page.request.get(base + '/health')).json();
        if (report.fixture.fixture !== 'web-push-synthetic') throw new Error('Unexpected fixture');
        await page.goto(base + '/von/');
        await page.waitForFunction(() => !/Checking/.test(document.querySelector('[data-push-status]').textContent), null, { timeout: 15000 });
        report.registration = await page.evaluate(async () => {
            const reg = await navigator.serviceWorker.ready;
            return { scope: reg.scope, scriptURL: reg.active.scriptURL, permission: Notification.permission };
        });
        report.initial_status = await page.locator('[data-push-status]').textContent();
        if (await page.locator('[data-push-enable]').isEnabled()) {
            await page.locator('[data-push-enable]').click();
            await page.waitForFunction(() => {
            const text = document.querySelector('[data-push-status]').textContent;
            return /Registration failed|push service|Waiting for|enabled|failed|denied|AbortError/i.test(text);
            }, null, { timeout: 25000 }).catch(() => {});
        } else {
            // Diagnose the actual browser transport without changing product
            // permission handling or injecting a synthetic Push event.
            report.transport_probe = await page.evaluate(async () => {
                const permission = Notification.permission;
                const permissionQuery = (await navigator.permissions.query({ name: 'notifications' })).state;
                const config = await (await fetch('/api/notifications/status')).json();
                const reg = await navigator.serviceWorker.ready;
                try {
                    const key = Uint8Array.from(atob(config.public_key.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
                    await Promise.race([
                        reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key }),
                        new Promise((_, reject) => setTimeout(() => reject(new Error('Transport probe exceeded 20 seconds')), 20000))
                    ]);
                    return { permission, permissionQuery, subscribed: true };
                } catch (error) {
                    return { permission, permissionQuery, error: error.name + ': ' + error.message };
                }
            });
        }
        report.observed_status = await page.locator('[data-push-status]').textContent();
        report.subscribed = await page.evaluate(async () => !!(await (await navigator.serviceWorker.ready).pushManager.getSubscription()));
        await page.screenshot({ path: path.join(evidence, 'controls.png'), fullPage: true });
        fs.writeFileSync(path.join(evidence, 'receipt.json'), JSON.stringify(report, null, 2) + '\n');
        console.log(JSON.stringify(report, null, 2));
    } finally {
        await context.close(); await browser.close();
    }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
