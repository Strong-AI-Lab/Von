// Run only against ontologyWriteFixture.py: this creates disposable concepts.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/ontologyWriteVerification.cjs URL EVIDENCE_DIR
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium, expect } = require('@playwright/test');
const [base = 'http://127.0.0.1:5099', evidence = '.run/ontology-verification'] = process.argv.slice(2);
assert(['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
fs.mkdirSync(evidence, { recursive: true });
(async () => {
    const browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const writes = [];
    page.on('request', r => { if (r.method() === 'PATCH' || (r.method() === 'POST' && r.url().endsWith('/api/concepts/'))) writes.push({url:r.url(), method:r.method(), data:r.postDataJSON()}); });
    page.on('dialog', d => d.accept());
    try {
        const health = await (await page.request.get(`${base}/health`)).json();
        assert.equal(health.agent_test_instance, true);
        await page.goto(`${base}/von/`);
        await page.locator('#vonBrowserTestLoginButton').click();
        await expect(page.locator('[data-tab="chatTab"]')).toBeVisible();
        const auth = await page.evaluate(async () => (await fetch('/von/api/auth/status')).json());
        assert.equal(auth.authenticated, true);
        assert.equal(auth.auth_provider, 'browser_test_fixture');
        assert.equal(auth.user_concept_id, '#V#ontology_fixture_author');
        const results = [];
        for (const scope of ['personal', 'sail']) {
            if (scope === 'sail') {
                await page.locator('.footer-org-menu-trigger').click();
                await page.locator('.footer-org-option[data-organisation-concept-id="#V#ontology_fixture_sail"]').click();
                await expect(page.locator('.footer-org-menu-trigger')).not.toHaveText('Personal');
            }
            await expect(async () => {
                await page.evaluate(() => document.dispatchEvent(new CustomEvent('von:selectConceptById', {
                    detail: { conceptId:'#V#thing', createConceptTab:true }
                })));
                await expect(page.locator('.tab-button[data-concept-id="#V#thing"]')).toBeVisible({timeout:1000});
            }).toPass({timeout:15000});
            await page.locator('.tab-button[data-concept-id="#V#thing"]').click();
            const name = `Browser verification ${scope}`;
            await page.locator('[id^="newInstanceInput_"]:visible').fill(name);
            const createdResponse = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/api/concepts/'));
            await page.locator('[id^="createInstanceButton_"]:visible').click();
            const response = await createdResponse;
            const created = await response.json();
            assert.equal(response.status(), 201, JSON.stringify(created));
            assert.equal(created.canonical_read_back.verified, true);
            const cid = created.concept.concept_id;
            await page.evaluate(conceptId => document.dispatchEvent(new CustomEvent('von:selectConceptById', {
                detail: { conceptId, createConceptTab:true }
            })), cid);
            await page.locator(`.tab-button[data-concept-id="${cid}"]`).click();
            const description = `## ${scope} fixture\n\nExact body with  two spaces.\n\n- Evidence retained`;
            for (let edit = 0; edit < 2; edit++) {
                await page.locator('[id^="typeEditDescriptionButton_"]:visible').click();
                await page.locator('[id^="typeDescriptionTextarea_"]:visible').fill(description);
                const savedResponse = page.waitForResponse(r => r.request().method() === 'PATCH' && r.url().endsWith('/description'));
                await page.locator('[id^="typeEditDescriptionSave_"]:visible').click();
                const saved = await savedResponse;
                assert.equal(saved.status(), 200, await saved.text());
                await expect(page.locator('[id^="typeDescriptionStatus_"]:visible')).toHaveText('Saved');
            }
            await page.screenshot({path:path.join(evidence, `${scope}-saved.png`)});
            results.push({scope, concept_id:cid, description, create_receipt:created.authority_receipt});
        }
        const canonical = await (await page.request.get(`${base}/fixture/evidence`)).json();
        assert.equal(canonical.receipts.length, 6);
        for (const receipt of canonical.receipts) assert.equal(receipt.status, 'succeeded');
        for (const result of results) {
            const concept = canonical.concepts[result.concept_id];
            assert.deepEqual(concept.relationships['#V#specific_to_user'], [auth.user_concept_id]);
            assert(!concept.relationships['#V#specific_to_organisation']?.length);
            const rows = canonical.descriptions[result.concept_id];
            assert.equal(rows.length, 1);
            assert.equal(rows[0].text, result.description);
            assert.equal(rows[0].lang, 'en');
            assert.equal(rows[0].context.write_strategy, 'relations_only_v2');
            assert.equal(rows[0].provenance.source, 'update_concept_description');
        }
        // UI-only transport uncertainty: the backend really saves once, then
        // the intercepted response reports uncertainty. Backend divergence is
        // separately tested without response interception in the Python suite.
        await page.route('**/api/concepts/*/description', async route => {
            const response = await route.fetch();
            assert.equal(response.status(), 200);
            await route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({
                success:false, effect_status:'indeterminate', retryable:false,
                authority_receipt:{receipt_id:'omr_transport_fixture'}
            })});
        });
        await page.locator('[id^="typeEditDescriptionButton_"]:visible').click();
        const draft = 'Uncertain response draft retained';
        await page.locator('[id^="typeDescriptionTextarea_"]:visible').fill(draft);
        const writesBefore = writes.length;
        await page.locator('[id^="typeEditDescriptionSave_"]:visible').click();
        await expect(page.locator('[id^="typeDescriptionStatus_"]:visible')).toContainText('verification is incomplete');
        await expect(page.locator('[id^="typeDescriptionTextarea_"]:visible')).toHaveValue(draft);
        assert.equal(writes.length, writesBefore + 1);
        await page.screenshot({path:path.join(evidence,'indeterminate-draft.png')});
        fs.writeFileSync(path.join(evidence,'browser-evidence.json'), JSON.stringify({
            observed_at:new Date().toISOString(), health, auth, results, canonical, writes,
            indeterminate_ui:{synthetic_response:true, draft_retained:true, writes:1},
            scope:'Local authenticated fixture; in-memory storage; generation disabled; no public OAuth or deployment claim'
        },null,2));
        console.log(JSON.stringify({success:true, scopes:results.map(r=>r.scope), receipts:6, draftRetained:true}));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
