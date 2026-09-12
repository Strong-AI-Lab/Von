// Run: node tests/browser/responsiveShell.cjs [evidence-directory] [--tray-only]
// Production templates, CSS, layout and tray controller with synthetic content.
// This checks rendering and draft retention, not authentication or live sending.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const baseline = process.env.VON_LAYOUT_BASELINE;
const trayOnly = process.argv.includes('--tray-only');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = process.argv[2];
if (evidence) fs.mkdirSync(evidence, { recursive: true });
function template(name) {
    return fs.readFileSync(path.join(root, 'templates', name), 'utf8')
        .replace(/{% include '([^']+)' %}/g, (_, child) => template(child))
        .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '')
        .replace(/{{ url_for\('static', filename='([^']+)'\) }}/g, '/static/$1')
        .replace(/{%[\s\S]*?%}|{{[\s\S]*?}}/g, '');
}
const server = http.createServer((req, res) => {
    if (req.url === '/' || req.url === '/settings') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        const name = req.url === '/settings' ? 'settings_tab.html' : 'von_interface.html';
        res.end(template(name));
        return;
    }
    const file = path.resolve(root, `.${req.url}`);
    if (!file.startsWith(`${root}/static/`) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/javascript');
    if (baseline && req.url === '/static/styles.css') {
        return res.end(execFileSync('git', ['show', `${baseline}:src/frontend/web/von_interface/static/styles.css`]));
    }
    if (baseline && req.url === '/static/js/components/dynamicLayout.js') {
        const main = execFileSync('git', ['show', `${baseline}:src/frontend/web/von_interface/static/js/main.js`], { encoding: 'utf8' });
        const start = main.indexOf('function setupDynamicLayout()');
        return res.end('export ' + main.slice(start, main.indexOf('\n}', start) + 2));
    }
    res.end(fs.readFileSync(file));
});
async function noOverflow(page) {
    const size = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth }));
    assert(size.document <= page.viewportSize().width + 1, `global overflow: ${JSON.stringify(size)}`);
}
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true });
    try {
        for (const profile of [
            { name: 'pixel-8', width: 412, height: 915, touch: true },
            { name: 'small-phone', width: 320, height: 740, touch: true },
            { name: 'folded', width: 360, height: 740, touch: true },
            { name: 'tablet', width: 768, height: 1024, touch: true },
            { name: 'unfolded', width: 840, height: 900, touch: true },
            { name: 'landscape', width: 915, height: 412, touch: true },
            { name: 'desktop', width: 1440, height: 900 }
        ].filter(profile => !baseline || profile.name === 'desktop')) {
            const page = await browser.newPage({ viewport: profile, hasTouch: !!profile.touch, isMobile: !!profile.touch });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await noOverflow(page);
            await expect(page.locator('#vonGoogleLoginButton')).toBeVisible();
            const preparePage = () => page.evaluate(async () => {
                // Synthetic signed-in presentation; no credentials or identity bypass.
                document.body.classList.remove('von-auth-pending');
                document.querySelector('#vonAuthenticationGate').hidden = true;
                document.querySelector('#vonAuthenticatedApp').hidden = false;
                document.querySelector('#headerOrgName').textContent = 'Research organisation with a long name';
                document.querySelector('#modelInfoFooter').innerHTML = `<span class="footer-segment">User: <button class="concept-footer-button">Acceptance user</button></span>
                    <span class="footer-org-switcher"><span class="footer-org-control"><button class="footer-org-current-button">Research organisation</button>
                    <details class="footer-org-menu-details"><summary class="footer-org-menu-trigger" aria-label="Switch organisation">▾</summary>
                    <div class="footer-org-menu"><button class="footer-org-option">Personal</button><button class="footer-org-option">Research organisation</button></div></details></span></span>
                    <span class="footer-segment">Model: configured model</span>`;
                document.querySelector('#chatSessionTabs').innerHTML = Array.from({ length: 12 }, (_, i) =>
                    `<button class="chat-session-tab${i === 10 ? ' is-active' : ''}" role="tab" aria-selected="${i === 10}"><span class="chat-session-tab-label">Saved research conversation ${i + 1}</span></button>`).join('');
                document.querySelector('#chatSessionHistoryControls').innerHTML = '<button>Older conversations</button><button>New conversation</button>';
                document.querySelector('#scrollableField').innerHTML = Array.from({ length: 25 }, (_, i) =>
                    `<p>Turn ${i + 1}: A readable saved research conversation with provenance and next steps.</p>`).join('');
                const workspace = document.querySelector('#conversationWorkspace');
                const { createConversationTray } = await import('/static/js/components/conversationTray.js');
                const { CONVERSATION_NARROW_QUERY, loadConversationLayoutPreferences } = await import('/static/js/utils/conversationLayoutPreferences.js');
                const tray = createConversationTray(workspace);
                const refresh = () => {
                    workspace.dataset.effectiveTabsLayout = matchMedia(CONVERSATION_NARROW_QUERY).matches ? 'horizontal' : 'vertical';
                    tray.refresh(loadConversationLayoutPreferences());
                };
                refresh();
                window.addEventListener('resize', refresh);
                const { setupDynamicLayout } = await import('/static/js/components/dynamicLayout.js');
                setupDynamicLayout();
            });
            await preparePage();
            await page.waitForTimeout(100);
            await noOverflow(page);
            const input = page.locator('#promptInput');
            await input.fill('Draft survives resizing and folding.');
            await page.locator('#sendButton').click({ trial: true });
            await expect(input).toHaveValue('Draft survives resizing and folding.');
            await page.locator('.footer-org-menu-trigger').click();
            await page.getByRole('button', { name: 'Personal', exact: true }).click({ trial: true });
            const menu = await page.locator('.footer-org-menu').boundingBox();
            assert(menu.x >= 0 && menu.x + menu.width <= profile.width + 1 && menu.y >= 0, 'organisation menu fits');
            await page.locator('.footer-org-menu-trigger').click();
            if (evidence) await page.screenshot({ animations: 'disabled', path: path.join(evidence, `${profile.name}.png`), fullPage: false });
            if (profile.name === 'desktop') {
                const toggle = page.locator('#conversationTrayToggle');
                const selected = page.locator('#chatSessionTabs [role="tab"]').first();
                await selected.evaluate(el => {
                    el.setAttribute('aria-selected', 'true');
                    el.classList.add('has-unread');
                    el.dataset.unreadCount = '3';
                });
                const retained = await page.evaluateHandle(() => document.querySelector('#scrollableField').firstChild);
                await toggle.focus();
                await page.keyboard.press('Enter');
                await expect(toggle).toHaveAttribute('aria-expanded', 'false');
                await expect(toggle).toHaveAccessibleName('Open conversation list');
                await page.keyboard.press('Space');
                await expect(toggle).toHaveAttribute('aria-expanded', 'true');
                await selected.focus();
                await page.keyboard.press('Escape');
                await expect(toggle).toBeFocused();
                await expect(toggle).toHaveAttribute('aria-expanded', 'false');
                // Hover uses the same grid track, never an overlay on the transcript.
                await toggle.hover();
                await expect(page.locator('#conversationWorkspace')).toHaveAttribute('data-tray-peek', 'true');
                await page.waitForTimeout(220);
                const trayBox = await page.locator('.chat-session-tabs-row').boundingBox();
                const mainBox = await page.locator('.chat-conversation-main').boundingBox();
                assert(trayBox.x + trayBox.width <= mainBox.x, 'expanded tray does not cover conversation');
                await page.keyboard.press('Escape');
                await page.keyboard.press('Enter');
                await expect(toggle).toHaveAttribute('aria-expanded', 'true');
                await expect(selected).toHaveAttribute('aria-selected', 'true');
                await expect(selected).toHaveAttribute('data-unread-count', '3');
                assert(await retained.evaluate(el => el === document.querySelector('#scrollableField').firstChild), 'transcript retained');
                await expect(input).toHaveValue('Draft survives resizing and folding.');
                await noOverflow(page);
                if (evidence) await page.screenshot({ path: path.join(evidence, 'desktop-tray-reopened.png') });
            }
            if (profile.touch) {
                const toggle = page.locator('#conversationTrayToggle');
                const context = page.locator('#conversationTrayContext');
                const selected = page.locator('#chatSessionTabs [aria-selected="true"]');
                await expect(context).toHaveText('Current: Saved research conversation 11');
                await expect(toggle).toHaveAttribute('aria-expanded', 'true');
                const listBox = await page.locator('#chatSessionTabs').boundingBox();
                const activeBox = await selected.boundingBox();
                assert(activeBox.x >= listBox.x - 1 && activeBox.x + activeBox.width <= listBox.x + listBox.width + 1, 'active conversation revealed without reordering');
                const longTitle = 'Research conversation with a deliberately long title about observations, provenance and the next collaborative steps';
                await selected.locator('.chat-session-tab-label').evaluate((el, title) => { el.textContent = title; }, longTitle);
                await expect(context).toHaveText(`Current: ${longTitle}`);
                await noOverflow(page);
                await selected.locator('.chat-session-tab-label').evaluate(el => { el.textContent = 'Saved research conversation 11'; });
                await expect(context).toHaveText('Current: Saved research conversation 11');
                const contextBox = await context.boundingBox();
                assert(contextBox.y + contextBox.height <= (await selected.boundingBox()).y, 'current context above list');
                const retained = await page.evaluateHandle(() => document.querySelector('#scrollableField').firstChild);
                await selected.focus();
                const scrollY = await page.evaluate(() => window.scrollY);
                await page.keyboard.press('Escape');
                await expect(toggle).toBeFocused();
                await expect(toggle).toHaveAccessibleName('Open conversation list');
                await expect(page.locator('#chatSessionTabs')).toBeHidden();
                await expect(context).toBeVisible();
                await page.keyboard.press('Enter');
                await expect(page.locator('#chatSessionTabs')).toBeVisible();
                assert.equal(await page.evaluate(() => window.scrollY), scrollY, 'reopening list leaves document scroll unchanged');
                await page.keyboard.press('Space');
                await expect(page.locator('#chatSessionTabs')).toBeHidden();
                assert(await retained.evaluate(el => el === document.querySelector('#scrollableField').firstChild), 'mobile transcript retained');
                await expect(input).toHaveValue('Draft survives resizing and folding.');
                await noOverflow(page);
                if (evidence) await page.screenshot({ path: path.join(evidence, `${profile.name}-tray-collapsed.png`) });
                await page.reload();
                await preparePage();
                await expect(toggle).toHaveAttribute('aria-expanded', 'false');
                await expect(context).toHaveText('Current: Saved research conversation 11');
                await toggle.click();
                await expect(selected).toBeVisible();
                await input.fill('Draft survives resizing and folding.');
                if (evidence) await page.screenshot({ path: path.join(evidence, `${profile.name}-tray-reopened.png`) });
            }
            if (trayOnly) {
                console.log(JSON.stringify({ profile: profile.name, passed: true, scope: 'conversation tray', source: 'synthetic production-template fixture' }));
                await page.close();
                continue;
            }
            if (profile.name === 'pixel-8') {
                await page.setViewportSize({ width: 412, height: 430 });
                await input.focus();
                await page.locator('#sendButton').click({ trial: true });
                await expect(input).toHaveValue('Draft survives resizing and folding.');
                await noOverflow(page);
                const send = await page.locator('#sendButton').boundingBox();
                const footer = await page.locator('.footer-container').boundingBox();
                assert(send.y >= 0 && send.y + send.height <= footer.y, 'keyboard-sized viewport keeps send above footer');
                if (evidence) await page.screenshot({ animations: 'disabled', path: path.join(evidence, 'pixel-8-keyboard.png') });
                await page.setViewportSize({ width: 840, height: 900 });
                await expect(input).toHaveValue('Draft survives resizing and folding.');
                await page.setViewportSize({ width: 412, height: 915 });
                await expect(input).toHaveValue('Draft survives resizing and folding.');
            }
            await page.evaluate(markup => {
                document.querySelectorAll('.tab-content.active').forEach(tab => tab.classList.remove('active'));
                const concept = document.createElement('div');
                concept.className = 'tab-content dynamic-concept-tab active';
                concept.innerHTML = markup;
                document.querySelector('.tab-content-area').append(concept);
                document.querySelector('#conceptFormTitleText').textContent = 'Research concept with a longer descriptive name';
            }, template('concept_tab.html'));
            await noOverflow(page);
            await page.locator('#discussConceptButton').click({ trial: true });
            if (evidence) await page.screenshot({ animations: 'disabled', path: path.join(evidence, `${profile.name}-concept.png`) });
            await page.goto(`http://127.0.0.1:${server.address().port}/settings`);
            await noOverflow(page);
            await expect(page.locator('#preferredLanguageSelect')).toBeVisible();
            if (evidence) await page.screenshot({ animations: 'disabled', path: path.join(evidence, `${profile.name}-settings.png`) });
            console.log(JSON.stringify({ profile: profile.name, passed: true, source: 'synthetic production-template fixture' }));
            await page.close();
        }
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
