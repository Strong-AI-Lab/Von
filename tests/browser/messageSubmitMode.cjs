// Actual message pane/modules/CSS with isolated synthetic HTTP. No live messages,
// authentication or agent requests; this is presentation and request-intent evidence.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2] || '.run/message-submit-mode';
fs.mkdirSync(evidence, { recursive: true });
let submitted = [];
const server = http.createServer(async (req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><div id="conversationWorkspace" class="show-message-exchange"><div id="messagesContainer"></div></div>');
    }
    if (req.url.startsWith('/static/')) {
        const file = path.resolve(root, `.${req.url}`);
        if (file.startsWith(root + '/static/') && fs.existsSync(file)) {
            res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
            return res.end(fs.readFileSync(file));
        }
    }
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/messages/' && req.method === 'POST') {
        let body = ''; for await (const chunk of req) body += chunk;
        submitted.push(JSON.parse(body));
        res.statusCode = 201;
        return res.end(JSON.stringify({ success: true, message_id: '#V#fixture_message', submit_mode: 'queue' }));
    }
    res.end(JSON.stringify({ success: true, authenticated: true, current_user_id: '#V#alice', messages: [], profiles: [] }));
});
async function mount(page) {
    await page.evaluate(async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#alice' }));
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#lab' }));
        const { openMessageExchange } = await import('/static/js/components/messagePanel.js');
        await openMessageExchange({ session_id: 'fixture', viewer_id: '#V#alice',
            participant_ids: ['#V#alice', '#V#worker'], other_participant_ids: ['#V#worker'],
            organisation_concept_id: '#V#lab', session_name: 'Coding agent' });
    });
}
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    try {
        const results = [];
        for (const width of [390, 1280]) {
            submitted = [];
            const touch = width < 800;
            const page = await browser.newPage({ viewport: { width, height: 850 }, hasTouch: touch });
            page.setDefaultTimeout(10000);
            const errors = []; page.on('pageerror', error => errors.push(error.message));
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await mount(page);
            const input = page.locator('#messageInput');
            const button = page.locator('#sendMessageBtn');
            const menu = page.locator('#messageComposeArea .message-submit-actions');
            await input.fill('Keep my multiline\ndraft unchanged.');
            await expect(button).toHaveAttribute('data-submit-mode', 'queue');
            const bounds = await button.boundingBox();
            assert(bounds.width >= 44 && bounds.height >= 44);
            await expect(button.locator('svg')).toBeVisible();
            await button.click({ modifiers: ['Shift'] });
            assert.equal(submitted.length, 0);
            await expect(input).toHaveValue('Keep my multiline\ndraft unchanged.');
            await menu.locator('summary')[touch ? 'tap' : 'click']();
            await menu.locator('select').selectOption('steer');
            await expect(button).toHaveAccessibleName(/Steering unavailable/);
            const panelBounds = await menu.locator('.conversation-actions-panel').boundingBox();
            assert(panelBounds.x >= 0 && panelBounds.x + panelBounds.width <= width);
            await page.screenshot({ path: path.join(evidence, `options-${width}.png`) });
            await menu.locator('select').focus(); await page.keyboard.press('Escape');
            await expect(menu.locator('summary')).toBeFocused();
            await button[touch ? 'tap' : 'click']();
            assert.equal(submitted.length, 0);
            await expect(input).toHaveValue('Keep my multiline\ndraft unchanged.');
            await page.reload(); await mount(page);
            await expect(button).toHaveAttribute('data-submit-mode', 'steer');
            await input.fill('Separate queued request.');
            await menu.locator('summary')[touch ? 'tap' : 'click']();
            await menu.getByRole('button', { name: 'Queue this message', exact: true })[touch ? 'tap' : 'click']();
            await expect(input).toHaveValue('');
            assert.equal(submitted.length, 1);
            assert.equal(submitted[0].submit_mode, 'queue');
            assert.deepEqual(submitted[0].recipient_ids, ['#V#worker']);
            assert.equal(submitted[0].organisation_concept_id, '#V#lab');
            await expect(button).toHaveAttribute('data-submit-mode', 'steer');
            // Multiline / IME behaviour remains owned by the existing composer.
            await input.fill('One'); await input.press('Shift+Enter');
            await expect(input).toHaveValue('One\n');
            assert.equal(submitted.length, 1);
            assert.deepEqual(errors, []);
            results.push({ width, touch, bounds, panelBounds, savedDefaultAfterReload: true,
                unsupportedDraftRetained: true, queuedPayload: submitted[0], noLiveDeliveryClaim: true });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify({ fixture: true, results }, null, 2));
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
