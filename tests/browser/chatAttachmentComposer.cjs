// Use only the operator-prepared disposable fixture; never production actors.
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/tmp/von-playwright';
const { chromium, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const output = process.argv[2] || '.run/attachments';
const url = process.env.VON_ATTACHMENT_ALICE_URL || 'http://127.0.0.1:5083';
const actor = process.env.VON_ATTACHMENT_ALICE || '#V#blocked_attachments650_alice';
const organisation = process.env.VON_ATTACHMENT_ORG_NAME || 'Blocked Attachments650 Organisation';
const png = fs.readFileSync(path.join(__dirname, 'fixtures', 'attachment.png'));
const note = Buffer.from('Chat attachment sample 73.');
const caption = 'Describe the attached image colour and report the sample number from the attached text. Keep the answer brief.';
const report = { method: 'Authenticated disposable fixture, actual generation/history; synthetic clipboard/drop events with original bytes', turns: [] };
fs.mkdirSync(output, { recursive: true });
const save = () => fs.writeFileSync(path.join(output, 'chat-result.json'), JSON.stringify(report, null, 2));
async function selectOrganisation(page) {
    await expect(page.locator('#promptInput')).toBeEnabled();
    const option = page.locator('.footer-org-option').filter({ hasText: organisation });
    await expect(option).toHaveCount(1);
    if (await option.getAttribute('aria-checked') !== 'true') {
        await page.locator('.footer-org-menu-trigger').click();
        await option.click();
    }
    await expect(option).toHaveAttribute('aria-checked', 'true');
}
async function fileEvent(page, kind, name) {
    await page.locator('#promptInput').evaluate((el, { kind, name, bytes }) => {
        const data = new DataTransfer();
        data.items.add(new File([new Uint8Array(bytes)], name, { type: 'image/png' }));
        el.dispatchEvent(kind === 'paste'
            ? new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true })
            : new DragEvent('drop', { dataTransfer: data, bubbles: true, cancelable: true }));
    }, { kind, name, bytes: [...png] });
    await expect(page.locator('#conversationImageComposer')).toContainText(name);
    await expect(page.locator('#conversationImageComposer')).not.toContainText('Uploading');
}
async function readHistory(page, sessionId) {
    return page.evaluate(async sessionId => {
        const { getWindowSessionId, WINDOW_SESSION_HEADER } = await import('/static/js/apiService.js');
        const r = await fetch(`/von/history?session_id=${encodeURIComponent(sessionId)}&segments=10&include_debug=0`, {
            headers: { [WINDOW_SESSION_HEADER]: getWindowSessionId() },
        });
        if (!r.ok) throw new Error(`History read failed: ${r.status}`);
        return r.json();
    }, sessionId);
}
async function verifyHistory(page, receipt, expected) {
    const history = await readHistory(page, receipt.session_id);
    const matching = history.history.filter(turn => turn.role === 'user'
        && turn.image_attachments?.some(a => a.concept_id === receipt.attachment_ids[0]));
    assert.equal(matching.length, 1, 'Exactly one user turn must own the sent attachments');
    const turn = matching[0];
    assert.deepEqual(turn.image_attachments.map(a => a.concept_id).sort(), [...receipt.attachment_ids].sort());
    assert.equal(turn.content, receipt.caption);
    for (const descriptor of turn.image_attachments) {
        const original = expected[descriptor.filename];
        assert.ok(original, `Unexpected file: ${descriptor.filename}`);
        const response = await page.request.get(`${url}/von/api/images/${encodeURIComponent(descriptor.concept_id)}/original`);
        assert.equal(response.status(), 200);
        assert.deepEqual(await response.body(), original);
        assert.equal(descriptor.sha256, crypto.createHash('sha256').update(original).digest('hex'));
    }
    receipt.history_original_bytes_verified = true;
    receipt.history_user_turn_count = matching.length;
}
(async () => {
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage();
        page.setDefaultTimeout(20000);
        await page.goto(`${url}/von/`);
        report.health = await page.evaluate(async () => (await fetch('/health')).json());
        assert.equal(report.health.agent_test_instance, true);
        if (process.env.VON_ATTACHMENT_EXPECTED_SHA) {
            assert.equal(report.health.version_details.git_commit, process.env.VON_ATTACHMENT_EXPECTED_SHA);
        }
        const login = page.waitForResponse(r => r.url().includes('/api/auth/') && r.request().method() === 'POST');
        await page.locator('#vonBrowserTestLoginButton').click();
        const response = await login;
        if (response.status() !== 200) throw new Error(await response.text());
        await expect.poll(() => page.evaluate(async () => {
            const auth = await (await fetch('/von/api/auth/status')).json();
            return auth.authenticated && auth.user_concept_id;
        }).catch(() => null)).toBe(actor);
        report.authenticated_actor = actor;
        await selectOrganisation(page);
        // The fixture operator must enable this model for the synthetic actor.
        // Luna's Chat Completions tool route requires reasoning_effort=none.
        await page.evaluate(async () => {
            const m = await import('/static/js/utils/localModelPreferences.js');
            m.setStoredOpenAiSelectedModel('gpt-5.6-luna');
            m.setStoredOpenAiModelParameters({ reasoning_effort: 'none' });
            m.setLocalPremiumModelUseEnabled(true);
        });
        await page.getByRole('button', { name: 'New conversation', exact: true }).click();
        await expect(page.locator('#promptInput')).toBeEnabled();
        await page.locator('#promptInput').fill(caption);
        await fileEvent(page, 'paste', 'chat-paste.png');
        await fileEvent(page, 'drop', 'chat-drop.png');
        await page.route('**/api/files/upload', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"Injected upload failure"}' }));
        await page.locator('#uploadFileInput').setInputFiles({ name: 'chat-notes.txt', mimeType: 'text/plain', buffer: note });
        await expect(page.getByRole('button', { name: 'Retry upload of chat-notes.txt' })).toBeVisible();
        assert.equal(await page.locator('#promptInput').inputValue(), caption);
        await page.unroute('**/api/files/upload');
        await page.getByRole('button', { name: 'Retry upload of chat-notes.txt' }).click();
        await expect(page.locator('#conversationImageComposer')).not.toContainText('Uploading');
        await expect(page.getByRole('button', { name: 'Retry upload of chat-notes.txt' })).toHaveCount(0);
        report.recoverable_upload_retry = true;
        await page.locator('#uploadFileInput').setInputFiles({ name: 'remove-before-send.txt', mimeType: 'text/plain', buffer: Buffer.from('Remove this draft file') });
        await page.getByRole('button', { name: 'Remove remove-before-send.txt', exact: true }).click();
        await expect(page.locator('#conversationImageComposer')).not.toContainText('remove-before-send.txt');
        report.remove_before_send = true;
        await page.screenshot({ path: path.join(output, 'chat-desktop.png') });
        await page.setViewportSize({ width: 360, height: 800 });
        await page.locator('summary').filter({ hasText: 'More actions' }).click();
        await expect(page.locator('#uploadFileButton')).toBeVisible();
        await page.screenshot({ path: path.join(output, 'chat-narrow.png') });
        report.narrow_picker = true;
        await page.locator('summary').filter({ hasText: 'Close actions' }).click();
        for (const attachmentOnly of [false, true]) {
            if (attachmentOnly) {
                await page.locator('#promptInput').fill('');
                await fileEvent(page, 'paste', 'chat-only.png');
            }
            const sent = page.waitForResponse(r => r.url().includes('/von/generate') && r.request().method() === 'POST', { timeout: 240000 });
            await page.locator('#sendButton').click();
            const response = await sent;
            const body = await response.json();
            const request = response.request().postDataJSON();
            const receipt = {
                attachment_only: attachmentOnly, status: response.status(), success: body.success,
                terminal_status: body.terminal_status, response: body.response,
                session_id: body.session_id, request_id: body.request_id, caption: request.prompt,
                attachment_ids: request.image_attachment_ids, model: request.model,
                calls: body.llm_debug?.llm_interaction?.calls,
            };
            report.turns.push(receipt);
            save();
            assert.equal(response.status(), 200);
            assert.equal(body.success, true, body.response);
            assert.equal(body.terminal_status, 'completed');
            await expect(page.locator('#promptInput')).toHaveValue('');
            await expect(page.locator('#conversationImageComposer')).toBeEmpty();
            if (!attachmentOnly) {
                assert.match(body.response, /blue/i);
                assert.match(body.response, /73/);
            }
            await verifyHistory(page, receipt, attachmentOnly
                ? { 'chat-only.png': png }
                : { 'chat-paste.png': png, 'chat-drop.png': png, 'chat-notes.txt': note });
        }
        const sessionId = report.turns[0].session_id;
        await page.reload();
        await selectOrganisation(page);
        await page.locator(`.chat-session-tab[data-session-id="${sessionId}"]`).click();
        await expect(page.locator('.conversation-image-gallery img[alt="chat-paste.png"]')).toBeVisible();
        await expect(page.locator('.conversation-image-gallery img[alt="chat-only.png"]')).toBeVisible();
        await expect(page.locator('.conversation-image-gallery').getByRole('link', { name: /chat-notes.txt/ })).toBeVisible();
        for (const receipt of report.turns) await verifyHistory(page, receipt, receipt.attachment_only
            ? { 'chat-only.png': png }
            : { 'chat-paste.png': png, 'chat-drop.png': png, 'chat-notes.txt': note });
        report.refresh_persistence = true;
        await page.screenshot({ path: path.join(output, 'chat-refreshed.png') });
        save();
        console.log(JSON.stringify({ passed: true, turns: report.turns.length, refresh_persistence: true }));
    } catch (error) {
        report.error = String(error);
        save();
        throw error;
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
