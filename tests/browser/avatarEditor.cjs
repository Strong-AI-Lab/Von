// Real editor/CSS with real detector-produced fixtures. API persistence and the
// image provider are isolated stubs; this is not public OAuth or live generation.
// First: pdm run python -m tests.browser.avatarFixtures DIR (or PYTHONPATH=. path)
// Then: PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/avatarEditor.cjs DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const root = path.resolve(__dirname, '../../src/frontend/web/von_interface');
const evidence = path.resolve(process.argv[2]);
const fixtures = JSON.parse(fs.readFileSync(path.join(evidence, 'prepared.json')));
const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
        res.setHeader('Content-Type', 'text/html');
        return res.end('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/css/participantProfile.css"><button id="open">Profile and avatar</button><script type="module">import {openParticipantProfile} from "/static/js/components/participantProfile.js"; document.querySelector("#open").onclick=()=>openParticipantProfile();</script>');
    }
    const file = pathname.startsWith('/fixture/') ? path.join(evidence, path.basename(pathname)) : path.resolve(root, `.${pathname}`);
    if ((!file.startsWith(`${root}/static/`) && !file.startsWith(`${evidence}/`)) || !fs.existsSync(file)) return res.writeHead(404).end();
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : file.endsWith('.png') ? 'image/png' : 'text/javascript');
    res.end(fs.readFileSync(file));
});
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch();
    const results = [];
    try {
        for (const width of [1440, 360]) {
            const page = await browser.newPage({ viewport: { width, height: 900 } });
            let chosen = 'single', saves = [], generations = [], uploads = 0, failGeneration = false;
            const profile = { concept_id: '#V#alice', display_name: 'Alice fixture', can_edit: true, available_scopes: ['user_only_default', 'global_general'] };
            await page.route('**/von/api/**', route => {
                const pathname = new URL(route.request().url()).pathname;
                let body;
                if (pathname.endsWith('/profile')) body = { profile };
                else if (pathname.endsWith('/prepare')) {
                    assert(route.request().headers()['x-von-window-session']);
                    assert(route.request().postDataBuffer().includes(Buffer.from('filename=')));
                    uploads++;
                    body = fixtures[chosen];
                } else if (pathname.endsWith('/generate')) {
                    const payload = route.request().postDataJSON();
                    generations.push(payload);
                    if (failGeneration) return route.fulfill({ status: 503, json: { error: 'fixture_unavailable', message: 'Image provider unavailable in this fixture.' } });
                    body = { image: { concept_id: '#V#derived', url: '/fixture/single.png' } };
                } else if (pathname.endsWith('/avatar')) {
                    saves.push(route.request().postDataJSON());
                    body = { profile: { ...profile, saved_scope: saves.at(-1).scope, saved_avatar_version: 'fixture' } };
                } else throw new Error(`Unexpected API: ${pathname}`);
                return route.fulfill({ json: body });
            });
            await page.goto(`http://127.0.0.1:${server.address().port}`);
            await page.getByRole('button', { name: 'Profile and avatar', exact: true }).click();
            const status = page.getByRole('status');
            const use = page.getByRole('button', { name: 'Use avatar', exact: true });
            await page.getByLabel('Upload avatar image').setInputFiles(path.join(evidence, 'single.png'));
            await expect(status).toContainText('Face framed');
            assert.equal(saves.length, 0);
            await page.screenshot({ path: path.join(evidence, `avatar-${width}-face.png`) });
            await page.getByLabel('Zoom', { exact: true }).fill('4');
            await page.getByLabel('Horizontal position').fill('20');
            await page.getByLabel('Vertical position').fill('30');
            await page.getByLabel('Avatar scope').selectOption('user_only_default');
            await use.click();
            await expect(status).toHaveText('Avatar saved.');
            assert.equal(saves[0].image_concept_id, '#V#single');
            assert.equal(saves[0].scope, 'user_only_default');
            assert.deepEqual(saves[0].crop, { x: 76.8, y: 115.2, size: 128 });
            for (const name of ['multiple', 'none', 'oriented']) {
                chosen = name;
                await page.locator('.participant-avatar-drop').dispatchEvent('drop', {
                    dataTransfer: await page.evaluateHandle(({ bytes, name }) => {
                        const dt = new DataTransfer();
                        dt.items.add(new File([new Uint8Array(bytes)], `${name}.png`, { type: 'image/png' }));
                        return dt;
                    }, { bytes: [...fs.readFileSync(path.join(evidence, `${name}.png`))], name })
                });
                await expect(status).toContainText(name === 'multiple' ? 'Multiple faces' : name === 'none' ? 'No face' : 'Face framed');
            }
            // EXIF-restored preview is the same framing as the upright reference.
            assert.deepEqual(fixtures.oriented.crop, fixtures.single.crop);
            await page.getByLabel('Avatar image description').fill('A watercolour portrait');
            await page.getByRole('button', { name: 'Generate from photo', exact: true }).click();
            await expect(status).toContainText('Preview ready');
            assert.equal(generations[0].source_image_concept_id, '#V#oriented');
            assert.equal(saves.length, 1);
            await use.click();
            await expect(status).toHaveText('Avatar saved.');
            assert.equal(saves[1].image_concept_id, '#V#derived');
            await page.getByRole('button', { name: 'Return to source photo' }).click();
            await expect(page.getByRole('group', { name: 'Adjust framing' })).toBeVisible();
            failGeneration = true;
            await page.getByRole('button', { name: 'Generate from photo' }).click();
            await expect(status).toHaveText('Image provider unavailable in this fixture.');
            await expect(use).toBeEnabled();
            await use.click();
            await expect(status).toHaveText('Avatar saved.');
            assert.equal(saves[2].image_concept_id, '#V#oriented');
            const layout = await page.locator('dialog').evaluate(el => ({ width: el.clientWidth, scrollWidth: el.scrollWidth, right: el.getBoundingClientRect().right, viewport: innerWidth }));
            assert(layout.scrollWidth <= layout.width + 1 && layout.right <= width);
            await page.screenshot({ path: path.join(evidence, `avatar-${width}-manual.png`) });
            results.push({ width, uploads, saves, generations, layout });
            await page.close();
        }
        fs.writeFileSync(path.join(evidence, 'browser-results.json'), JSON.stringify(results, null, 2));
        console.log('PASS: desktop/narrow picker, drop, face defaults, manual crop, orientation, source-conditioned request, explicit apply and recoverable provider failure');
    } finally { await browser.close(); server.close(); }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
