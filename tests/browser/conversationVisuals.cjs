// Real Chromium, actual chat renderer; the credential-free fixture is explicitly labelled.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
    const [url = 'http://127.0.0.1:5037', evidenceDir = '.run/conversation-visuals'] = process.argv.slice(2);
    await fs.mkdir(evidenceDir, { recursive: true });
    const browser = await chromium.launch({ headless: true });
    const receipt = { environment: 'credential-free presentation fixture', runs: [] };
    try {
        for (const [name, width, scheme] of [['desktop', 1360, 'light'], ['mobile', 390, 'light'], ['dark', 1360, 'dark']]) {
            const page = await browser.newPage({ viewport: { width, height: 1000 }, colorScheme: scheme });
            const errors = [];
            page.on('pageerror', e => errors.push(e.message));
            await page.goto(url);
            await page.waitForFunction(() => window.fixtureReady);
            if (scheme === 'dark') await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
            await page.waitForSelector('[data-turn-id="a-source"] .diagram svg');
            await page.waitForSelector('[data-turn-id="a-source"] .katex');
            await page.waitForFunction(() => [...document.querySelectorAll('[data-image-id] img')].length === 2
                && [...document.querySelectorAll('[data-image-id] img')].every(img => img.complete && img.naturalWidth === 320));
            const observed = await page.evaluate(() => {
                const source = document.querySelector('[data-turn-id="a-source"]');
                const mixed = document.querySelector('[data-turn-id="a-mixed"] .chat-retained-content');
                return { diagramLabels: [...source.querySelectorAll('.diagram svg text')].map(e => e.textContent).join(' '),
                    mathCount: source.querySelectorAll('.katex').length,
                    localErrors: source.querySelectorAll('.visual-error').length,
                    imageCount: document.querySelectorAll('[data-image-id] img').length,
                    imageAspectPreserved: [...document.querySelectorAll('[data-image-id] img')].every(img =>
                        Math.abs(img.getBoundingClientRect().width / img.getBoundingClientRect().height - img.naturalWidth / img.naturalHeight) < .02),
                    orderedParts: [...mixed.children].map(e => e.querySelector('img') ? 'image' : e.textContent.trim()),
                    codePreserved: source.querySelector('pre > code.language-python')?.textContent,
                    currencyPreserved: source.textContent.includes('$25 and $10'),
                    tableCount: source.querySelectorAll('table').length,
                    attack: Boolean(window.visualAttack),
                    pageOverflow: document.documentElement.scrollWidth > innerWidth + 2 };
            });
            const labelText = observed.diagramLabels.replace(/\s+/g, '');
            assert(labelText.includes('Organisationhome—initiallyDGXforSAIL'));
            assert(labelText.includes('Objectstorage:packagesandevidence'));
            assert.equal(observed.mathCount, 2);
            assert.equal(observed.localErrors, 2);
            assert.deepEqual(observed.orderedParts, ['Text before the image.', 'image', 'Text after the image.']);
            assert(observed.currencyPreserved && observed.codePreserved.includes('cost'));
            assert.equal(observed.tableCount, 1);
            assert.equal(observed.attack, false);
            assert.equal(observed.pageOverflow, false);
            assert.equal(observed.imageAspectPreserved, true);
            assert.deepEqual(errors, []);
            await page.evaluate(() => window.replayFixture());
            assert.equal(await page.locator('[data-image-id] img').count(), 2);
            await page.locator('[data-turn-id="a-source"] .diagram .visual-source summary').first().click();
            await page.locator('[data-turn-id="a-source"] .diagram .visual-source button').first().focus();
            const exactSource = (await (await page.request.get(`${url}/fixture/messages`)).json())[0].content.split('```mermaid\n')[1].split('\n```')[0] + '\n';
            assert.equal(await page.locator('[data-turn-id="a-source"] .diagram .visual-source code').first().textContent(), exactSource);
            if (scheme === 'dark') {
                const sourceColours = await page.locator('[data-turn-id="a-source"] .diagram .visual-source code').first().evaluate(el => {
                    const style = getComputedStyle(el); return [style.color, style.backgroundColor];
                });
                assert.deepEqual(sourceColours, ['rgb(229, 231, 235)', 'rgb(21, 32, 44)']);
            }
            await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
            await page.locator('[data-turn-id="a-source"] .diagram .visual-source button').first().click();
            assert.equal(await page.evaluate(() => navigator.clipboard.readText()), exactSource);
            await page.screenshot({ path: path.join(evidenceDir, `${name}.png`), fullPage: true });
            await page.evaluate(() => window.reviseDiagramFixture());
            await page.waitForSelector('[data-turn-id="a-revised-source"] .diagram svg');
            assert.equal(await page.locator('[data-turn-id="a-revised-source"] .diagram .visual-source code').first().textContent(),
                exactSource.replace('Organisation home — initially DGX for SAIL', 'Organisation home — research hub'));
            assert.equal(await page.locator('[data-turn-id="a-revised-source"] .diagram svg .node').count(), 7);
            assert.equal(await page.locator('[data-turn-id="a-source"] .diagram .visual-source code').first().textContent(), exactSource);
            const security = await page.evaluate(async () => {
                const { renderConversationVisuals } = await import('/static/js/conversationVisuals.js');
                const holder = document.createElement('div');
                holder.innerHTML = '<pre><code class="language-mermaid"></code></pre>';
                holder.querySelector('code').textContent = '%%{init: {"securityLevel":"loose","flowchart":{"htmlLabels":true}}}%%\nflowchart LR\n A["<img src=x onerror=window.visualAttack=true>"] --> B[Safe]\n click A "javascript:window.visualAttack=true"';
                document.body.appendChild(holder);
                await renderConversationVisuals(holder);
                const result = { executed: Boolean(window.visualAttack), svg: Boolean(holder.querySelector('svg')),
                    active: Boolean(holder.querySelector('.visual-output script, .visual-output foreignObject, .visual-output a, .visual-output [onerror], .visual-output [onclick]')) };
                holder.remove();
                return result;
            });
            assert.deepEqual(security, { executed: false, svg: true, active: false });
            await page.reload();
            await page.waitForSelector('[data-turn-id="a-source"] .diagram svg');
            await page.waitForFunction(() => document.querySelectorAll('[data-image-id] img').length === 2);
            receipt.runs.push({ name, width, scheme, ...observed, errors });
            await page.close();
        }
        const response = await fetch(`${url}/health`); receipt.candidate = await response.json();
        await fs.writeFile(path.join(evidenceDir, 'receipt.json'), JSON.stringify(receipt, null, 2));
        console.log(JSON.stringify(receipt));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
