// Run: node tests/browser/composerControls.cjs [evidence-directory]
// Local fixture: production template/CSS and prompt/dictation modules, no Von
// server, identity, model calls or microphone access. Send behaviour is covered
// by chatTabSpeechPlanning.test.js; this checks control placement and reachability.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');

const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const markup = fs.readFileSync(path.join(root, 'templates/chat_tab.html'), 'utf8')
    .replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
const evidence = process.argv[2];
if (evidence) fs.mkdirSync(evidence, { recursive: true });
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
            <link rel="stylesheet" href="/static/styles.css">${markup}`);
        return;
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) {
        res.writeHead(404).end();
        return;
    }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(fs.readFileSync(file));
});

async function bounds(page, selector) {
    return page.locator(selector).boundingBox();
}

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        for (const profile of [
            { name: 'desktop', width: 1440, desktop: true },
            { name: 'small-desktop', width: 900, desktop: true },
            { name: 'phone-small', width: 360, height: 640, desktop: false, touch: true },
            { name: 'phone-large', width: 412, height: 915, desktop: false, touch: true },
            { name: 'short-viewport', width: 360, height: 360, desktop: false, touch: true },
            { name: 'wide-touch', width: 1024, desktop: false, touch: true }
        ]) {
            const page = await browser.newPage({
                viewport: { width: profile.width, height: profile.height || 900 },
                hasTouch: !!profile.touch, isMobile: !!profile.touch
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.evaluate(async desktop => {
                document.querySelector('#messagesContainer').style.display = 'none';
                document.querySelector('#conversationWorkspace').dataset.effectiveTabsLayout = desktop ? 'vertical' : 'horizontal';
                document.querySelector('#chatSessionTabs').innerHTML = '<button class="chat-session-tab is-active">Composer acceptance conversation</button>';
                document.querySelector('#scrollableField').textContent = 'A saved conversation with a draft ready to send.';
                document.querySelector('#scrollableField').style.minHeight = '300px';
                const { initializePromptCartoucheOverlay } = await import('/static/js/components/promptCartoucheOverlay.js');
                const input = document.querySelector('#promptInput');
                initializePromptCartoucheOverlay(input);
                const { initialiseCompactChatComposer, resizeCompactDraft } = await import('/static/js/components/compactComposer.js');
                initialiseCompactChatComposer(document.querySelector('.chat-composer'));
                input.addEventListener('input', () => {
                    resizeCompactDraft(input);
                    document.querySelector('#sendButton').dataset.hasDraft = String(Boolean(input.value.trim()));
                });
                input.dispatchEvent(new Event('input'));
            }, profile.desktop);
            const input = page.locator('#promptInput');
            const send = page.getByRole('button', { name: 'Send Prompt', exact: true });
            const mic = page.getByRole('button', { name: 'Dictate', exact: true });
            const summary = page.locator('.chat-composer-more-actions>summary');
            await expect(summary).toHaveAccessibleName('More actions');
            if (!profile.desktop) {
                await expect(send).toBeHidden();
                assert((await bounds(page, '.chat-composer')).height < 80, 'compact idle input');
                if (evidence) await page.screenshot({ path: path.join(evidence, `${profile.name}-idle.png`) });
            }
            await input.fill('Draft for the interaction check');
            await input.focus();
            await page.keyboard.press('Tab');
            await expect(send).toBeFocused();
            await page.keyboard.press('Tab');
            if (profile.desktop) {
                await expect(mic).toBeFocused();
                await page.keyboard.press('Tab');
            }
            await expect(summary).toBeFocused();
            const draft = await bounds(page, '#promptInput');
            const sendBox = await send.boundingBox();
            const micBox = await page.locator('#dictateButton').boundingBox();
            const moreBox = await summary.boundingBox();
            const composer = await bounds(page, '.chat-composer');
            assert(composer.x >= 0 && composer.x + composer.width <= profile.width, 'composer stays inside viewport');
            await send.click({ trial: true });
            await summary.focus();
            if (profile.desktop) {
                for (const box of [sendBox, micBox, moreBox]) {
                    assert(box.x >= draft.x + draft.width - 1, `controls must be to the right of the draft: ${JSON.stringify({ draft, box })}`);
                    assert(Math.abs(box.y - draft.y) < 2, 'controls must share the draft row');
                    assert(box.width >= 44 && box.height >= 44, 'controls retain usable targets');
                }
                assert(composer.height < 80, 'idle composer must not reserve another control/status row');
            } else {
                assert(sendBox.y < draft.y + draft.height, 'phone Send shares the draft row');
                assert(sendBox.width >= 44 && sendBox.height >= 44, 'Send touch target');
                assert(moreBox.width >= 44 && moreBox.height >= 44, 'actions touch target');
                assert(composer.height < 110, 'typing remains compact');
                assert(micBox === null, 'idle microphone lives in the closed menu');
                await expect(page.locator('.composer-add-icon')).toBeVisible();
                await expect(page.locator('.composer-more-icon')).toBeHidden();
            }
            await page.keyboard.press('Enter');
            await expect(page.locator('.chat-composer-more-actions')).toHaveAttribute('open', '');
            await page.keyboard.press('Tab');
            if (profile.desktop) await expect(page.getByRole('button', { name: 'Start voice', exact: true })).toBeFocused();
            await expect(page.getByRole('button', { name: 'Upload File', exact: true })).toBeVisible();
            if (profile.desktop) {
                const panel = await bounds(page, '.chat-composer-more-actions>.button-row');
                assert(panel.y >= 0 && panel.x >= 0 && panel.x + panel.width <= profile.width, JSON.stringify({ panel, composer }));
                assert.equal((await bounds(page, '.chat-composer')).height, composer.height, 'menu must not expand desktop composer');
            }
            if (evidence) await page.screenshot({ path: path.join(evidence, `${profile.name}-menu.png`), fullPage: true });
            await summary.focus();
            await page.keyboard.press('Space');
            await expect(page.locator('.chat-composer-more-actions')).not.toHaveAttribute('open', '');

            // Bind the production dictation controller with a fake recorder.
            await page.evaluate(async () => {
                const { createDictationController } = await import('/static/js/dictation.js');
                class Recorder {
                    static isTypeSupported() { return true; }
                    constructor() { this.state = 'inactive'; }
                    start() { this.state = 'recording'; }
                    stop() { this.state = 'inactive'; this.onstop?.(); }
                }
                window.fixtureDictation = createDictationController({
                    input: document.querySelector('#promptInput'), button: document.querySelector('#dictateButton'),
                    status: document.querySelector('#dictationStatus'), cancelButton: document.querySelector('#cancelDictationButton'),
                    retryButton: document.querySelector('#retryDictationButton'), engineSelect: document.querySelector('#dictationEngineSelect'),
                    getContext: () => ({ key: 'composer-fixture' }), setValue: value => { document.querySelector('#promptInput').value = value; },
                    onStart: () => {}, onAlternateClick: () => { window.alternateMicClicked = true; },
                    root: { isSecureContext: true, MediaRecorder: Recorder, navigator: { mediaDevices: {
                        getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] })
                    } } },
                    fetchImpl: async () => ({ ok: true, json: async () => ({ transcription: { available: true } }) })
                });
            });
            if (!profile.desktop) await summary.click();
            await page.locator('#dictateButton').click();
            await expect(page.locator('#dictateButton')).toHaveAttribute('aria-pressed', 'true');
            await page.getByRole('button', { name: 'Finish dictation', exact: true }).click();
            await expect(page.locator('#dictationStatus')).toContainText('No audio was recorded');
            await expect(page.locator('#dictationStatus')).toBeVisible();
            if (!profile.desktop) await summary.click();
            await page.locator('#dictateButton').click();
            await expect(page.locator('#dictateButton')).toHaveAttribute('aria-pressed', 'true');
            await page.getByRole('button', { name: 'Cancel dictation', exact: true }).click();
            await expect(page.locator('#dictateButton')).toHaveAttribute('aria-pressed', 'false');
            if (!profile.desktop) await summary.click();
            await page.locator('#dictateButton').click({ modifiers: ['Shift'] });
            assert(await page.evaluate(() => window.alternateMicClicked));
            await expect(input).toHaveValue('Draft for the interaction check');
            await page.evaluate(() => window.fixtureDictation.dispose());
            if (!profile.desktop) {
                // Presentation projection of the real controller's active-voice state;
                // voiceConversation.test.js exercises its isolated lifecycle.
                await page.evaluate(() => {
                    const voice = document.querySelector('#voiceConversationButton');
                    voice.textContent = 'End voice'; voice.setAttribute('aria-pressed', 'true');
                });
                await expect(page.locator('.compact-composer-active > #voiceConversationButton')).toBeVisible();
                await page.evaluate(() => document.querySelector('#voiceConversationButton').setAttribute('aria-pressed', 'false'));
                await expect(page.locator('#voiceConversationButton')).toBeHidden();
            }
            console.log(JSON.stringify({ profile: profile.name, draft, send: sendBox, microphone: micBox, more: moreBox, composerHeight: composer.height, passed: true }));
            await page.close();
        }
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
