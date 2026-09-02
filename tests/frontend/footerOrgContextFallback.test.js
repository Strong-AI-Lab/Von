/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';

describe('footer org context fallback', () => {
    beforeEach(() => {
        document.body.innerHTML = '<span id="headerOrgName">...</span><div id="modelInfoFooter"></div>';
        localStorage.clear();
        localStorage.setItem('von_org_context', JSON.stringify({
            concept_id: '#V#the_lu_witbrock_household',
            name: 'The Lu Witbrock Household'
        }));

        global.fetch = jest.fn(async (url) => {
            const path = String(url);
            if (path === '/api/settings/') {
                return { ok: true, json: async () => ({ active_llm: null }) };
            }
            if (path === '/api/settings/llm/info') {
                return { ok: true, json: async () => ({}) };
            }
            if (path === '/api/settings/db/info') {
                return { ok: true, json: async () => ({}) };
            }
            return { ok: true, json: async () => ({}) };
        });
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
    });

    test('uses von_org_context to render org footer segment', async () => {
        const { setModelInfoFooterText, updateHeaderOrgName } = require(domUtilsPath);

        updateHeaderOrgName();
        await setModelInfoFooterText();

        expect(document.querySelector('#headerOrgName')?.textContent)
            .toBe('The Lu Witbrock Household');
        const orgSegment = Array.from(document.querySelectorAll('.footer-segment'))
            .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'Org:');
        expect(orgSegment).toBeTruthy();

        const orgButton = orgSegment.querySelector('.concept-footer-button');
        expect(orgButton).toBeTruthy();
        expect(orgButton.textContent).toContain('The Lu Witbrock Household');
    });
});
