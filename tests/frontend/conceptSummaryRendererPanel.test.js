/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
}));

describe('concept summary renderer panel', () => {
    let warnSpy;

    beforeEach(() => {
        document.body.innerHTML = `
            <div id="conceptStep1_test">
                <div id="conceptSummaryRendererMount_test" class="concept-summary-renderer-mount"></div>
            </div>
        `;
        jest.resetModules();
        jest.clearAllMocks();
        warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    });

    afterEach(() => {
        warnSpy.mockRestore();
    });

    test('renders the concept summary card and toggles expanded sections', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                success: true,
                selected_renderer: { renderer_id: '#V#concept_page_identity_renderer' },
                panel: {
                    variant: 'identity',
                    eyebrow: 'Person',
                    title: 'Ada Lovelace',
                    subtitle: 'Researcher · Analytical Engine Lab',
                    badges: ['Researcher', 'Computing pioneer'],
                    facts: [
                        { label: 'Email', value: 'ada@example.org' },
                        { label: 'Affiliation', value: 'Analytical Engine Lab' },
                    ],
                    summary: 'Mathematician and computing pioneer.',
                    expanded_sections: [
                        { title: 'Affiliations', items: ['Analytical Engine Lab'] },
                    ],
                },
            },
        });

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');

        const rendered = await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#ada',
            suffix: 'test',
        });

        expect(rendered).toBe(true);
        const panel = document.getElementById('conceptSummaryRendererPanel_test');
        expect(panel).not.toBeNull();
        expect(panel.querySelector('.concept-summary-title').textContent).toBe('Ada Lovelace');
        expect(panel.querySelector('.concept-summary-facts').textContent).toContain('ada@example.org');
        const toggle = panel.querySelector('.concept-summary-toggle');
        const expanded = panel.querySelector('.concept-summary-expanded');
        expect(expanded.classList.contains('hidden')).toBe(true);
        toggle.click();
        expect(expanded.classList.contains('hidden')).toBe(false);
        expect(toggle.textContent).toBe('Show less');
    });

    test('returns false when the summary renderer route is unavailable', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockRejectedValue(new Error('HTTP 500'));

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');

        const rendered = await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#missing',
            suffix: 'test',
        });

        expect(rendered).toBe(false);
        const panel = document.getElementById('conceptSummaryRendererPanel_test');
        expect(panel.classList.contains('hidden')).toBe(true);
    });
});
