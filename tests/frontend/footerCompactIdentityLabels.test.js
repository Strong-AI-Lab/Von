/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';

function findFooterSegment(label) {
    return [...document.querySelectorAll('.footer-segment')]
        .find((segment) => segment.querySelector('.footer-label-inline')?.textContent?.trim() === `${label}:`);
}

async function flushUiTicks(ticks = 4) {
    for (let index = 0; index < ticks; index += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
}

describe('footer compact identity labels', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<div class="footer-container"><p id="modelInfoFooter"></p></div>';
        localStorage.clear();
        sessionStorage.clear();
        localStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#michael_witbrock',
            name: 'michael.witbrock@example.test',
        }));
        sessionStorage.setItem('von_current_user', localStorage.getItem('von_current_user'));
        sessionStorage.setItem('von_current_org', JSON.stringify({
            concept_id: '#V#university_of_auckland_strong_ai_lab',
            name: 'University of Auckland Strong AI Lab',
        }));

        global.fetch = jest.fn(async (url) => {
            const parsed = new URL(String(url), 'http://localhost');
            if (parsed.pathname === '/api/settings/' || parsed.pathname === '/api/settings') {
                return { ok: true, json: async () => ({ resolved_llm: null }) };
            }
            if (parsed.pathname === '/api/settings/llm/info' || parsed.pathname === '/api/settings/db/info') {
                return { ok: true, json: async () => ({}) };
            }
            if (parsed.pathname === '/api/workflows/capability-index/status') {
                return { ok: true, json: async () => ({ ready: true, status: 'ready' }) };
            }
            if (parsed.pathname === '/von/api/auth/status' || parsed.pathname === '/api/auth/status') {
                return {
                    ok: true,
                    json: async () => ({ authenticated: true, email: 'michael.witbrock@example.test' }),
                };
            }
            if (parsed.pathname === '/von/api/organisations/my_organisations') {
                return {
                    ok: true,
                    json: async () => ({ organisations: [{
                        concept_id: '#V#university_of_auckland_strong_ai_lab',
                        name: 'University of Auckland Strong AI Lab',
                        role: 'owner',
                    }] }),
                };
            }
            if (parsed.pathname === '/api/concepts/%23V%23michael_witbrock') {
                return {
                    ok: true,
                    json: async () => ({ names: [
                        { name: 'Michael Witbrock', language: 'en-NZ', type: 'NL' },
                        { name: 'MJW', language: 'en-NZ', type: 'ABBR' },
                    ] }),
                };
            }
            if (parsed.pathname === '/api/concepts/%23V%23university_of_auckland_strong_ai_lab') {
                return {
                    ok: true,
                    json: async () => ({ names: [
                        { name: 'University of Auckland Strong AI Lab', language: 'en-NZ', type: 'NL' },
                        { name: 'SAIL', language: 'en-NZ', type: 'ABBR' },
                    ] }),
                };
            }
            return { ok: true, json: async () => ({}) };
        });
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
    });

    test('uses canonical abbreviations for the dense footer without losing full identity semantics', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();
        await flushUiTicks();

        const userButton = findFooterSegment('User')?.querySelector('.concept-footer-button');
        expect(userButton?.textContent).toBe('MJW');
        expect(userButton?.getAttribute('aria-label')).toContain('User: michael.witbrock@example.test');
        expect(userButton?.getAttribute('aria-label')).toContain('Compact label: MJW');

        const orgButton = findFooterSegment('Org')?.querySelector('.footer-org-current-button');
        expect(orgButton?.textContent).toBe('SAIL');
        expect(orgButton?.getAttribute('aria-label')).toContain('Organisation: University of Auckland Strong AI Lab');
        expect(orgButton?.getAttribute('aria-label')).toContain('Compact label: SAIL');
    });
});
