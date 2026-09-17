// Local Chromium component acceptance: actual attachment module, fixture transport.
// Does not use server authentication, upload real data, or invoke a model.
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/tmp/von-playwright';
const { chromium, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const output = process.argv[2] || '.run/attachment-lifecycle';
(async () => {
    fs.mkdirSync(output, { recursive: true });
    const browser = await chromium.launch({ headless: true });
    try {
        const page = await browser.newPage();
        await page.route('http://attachment.test/**', route => {
            if (route.request().url().endsWith('/module.js')) return route.fulfill({
                contentType: 'text/javascript',
                body: fs.readFileSync(path.join(__dirname, '../../src/frontend/web/von_interface/static/js/utils/conversationImages.js'), 'utf8')
            });
            return route.fulfill({ contentType: 'text/html', body: '<main><div id="attachments"></div><button id="send">Send</button><p id="status"></p></main>' });
        });
        await page.goto('http://attachment.test/');
        await page.evaluate(async () => {
            const api = await import('/module.js');
            const root = document.getElementById('attachments');
            const refresh = () => {
                api.renderImageComposer(root, 'draft', refresh);
                document.getElementById('send').disabled = api.imagesBlocked('draft');
                document.getElementById('status').textContent = api.imagesBlocked('draft') ? api.attachmentBlockMessage('draft') : 'Ready to send';
            };
            window.api = api;
            window.fetch = async (_, options) => {
                const name = options.body.get('file').name;
                if (name === 'program.pdf') return new Promise(resolve => { window.finish = resolve; });
                return { ok: true, json: async () => ({ uploaded: { concept_id: '#V#poster', sha256: 'fixture-sha' } }) };
            };
            await api.uploadConversationImage(new File(['poster'], 'poster.pdf', { type: 'application/pdf' }), 'draft', {}, refresh);
            window.slow = api.uploadConversationImage(new File(['program'], 'program.pdf', { type: 'application/pdf' }), 'draft', {}, refresh);
        });
        await expect(page.locator('[data-attachment-state="ready"]')).toContainText('poster.pdf');
        await expect(page.locator('[data-attachment-state="uploading"]')).toContainText('program.pdf');
        await expect(page.locator('#send')).toBeDisabled();
        await page.getByRole('button', { name: 'Remove program.pdf', exact: true }).click();
        await expect(page.locator('#send')).toBeEnabled();
        await expect(page.locator('#status')).toHaveText('Ready to send');
        await page.screenshot({ path: path.join(output, 'ready-after-removal.png') });
        const result = await page.evaluate(async () => {
            const ids = api.takeImages('draft');
            finish({ ok: true, json: async () => ({ uploaded: { concept_id: '#V#late' } }) });
            await slow;
            return { ids, descriptors: api.descriptorsForIds(ids), remaining: api.imageItems('draft').length };
        });
        expect(result.ids).toEqual(['#V#poster']);
        expect(result.descriptors[0]).toMatchObject({ filename: 'poster.pdf', content_type: 'application/pdf', size_bytes: 6, sha256: 'fixture-sha' });
        expect(result.remaining).toBe(0);
        fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ method: 'Chromium actual attachment component with local fixture transport', ...result }, null, 2));
        console.log('PASS: ready/uploading status, removal unblocks send, late completion ignored, metadata preserved');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
