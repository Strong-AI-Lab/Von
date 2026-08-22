/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/chatTab.js', () => ({
    switchToChatSession: jest.fn(),
}));

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

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

    test('renders server text as text rather than executable markup', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                panel: {
                    variant: 'identity',
                    eyebrow: '<img src=x onerror="window.__summaryXss = true">',
                    title: '<script>window.__summaryXss = true</script>',
                    badges: ['<img src=x onerror="window.__summaryXss = true">'],
                    facts: [{ label: '<b>Email</b>', value: '<svg onload="window.__summaryXss = true">' }],
                    summary: '<a href="javascript:window.__summaryXss = true">unsafe link</a>',
                    expanded_sections: [{ title: '<i>Details</i>', items: ['<iframe srcdoc="x"></iframe>'] }],
                },
            },
        });

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');

        await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#markup',
            suffix: 'test',
        });

        const panel = document.getElementById('conceptSummaryRendererPanel_test');
        expect(panel.querySelector('script, img, svg, a, iframe')).toBeNull();
        expect(panel.querySelector('.concept-summary-title').textContent).toBe(
            '<script>window.__summaryXss = true</script>',
        );
        expect(panel.textContent).toContain('<a href="javascript:window.__summaryXss = true">unsafe link</a>');
        expect(window.__summaryXss).toBeUndefined();
    });

    test('opens an allow-listed conversation and focal concept through reauthorising controllers', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const chatTab = require('../../src/frontend/web/von_interface/static/js/chatTab.js');
        const tabNavigation = require('../../src/frontend/web/von_interface/static/js/tabNavigation.js');
        chatTab.switchToChatSession.mockResolvedValue({ ok: true });
        api.getJsonDetailed.mockResolvedValue({
            data: {
                panel: {
                    variant: 'conversation',
                    eyebrow: 'Conversation',
                    title: 'Current discussion',
                    focal_concepts: [{
                        concept_id: '#V#focus',
                        display_name: 'Visible focus',
                    }],
                    open_action: {
                        kind: 'open_conversation',
                        session_id: 'session-123',
                    },
                },
            },
        });
        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener('von:selectConceptById', handler);

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');
        await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#conversation_123',
            suffix: 'test',
        });

        document.querySelector('.concept-summary-focus-link').click();
        document.querySelector('.concept-summary-open-conversation').click();
        await flushMicrotasks();

        expect(seen).toEqual([{
            conceptId: '#V#focus',
            createConceptTab: true,
            promoteExistingTab: true,
            modifierKeys: { shiftKey: true },
        }]);
        expect(chatTab.switchToChatSession).toHaveBeenCalledWith('session-123');
        expect(tabNavigation.activateTab).toHaveBeenCalledWith('chatTab');
        expect(document.querySelector('.concept-summary-open-conversation').disabled).toBe(false);
        document.removeEventListener('von:selectConceptById', handler);
    });

    test('renders backlink cards but omits unsupported or malformed actions', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                panel: {
                    variant: 'generic',
                    title: 'Project',
                    open_action: { kind: 'delete_conversation', session_id: 'unsafe' },
                },
                conversation_backlinks: {
                    items: [
                        {
                            title: 'Useful discussion',
                            open_action: { kind: 'open_conversation', session_id: 'shared-1' },
                            focal_concepts: [],
                        },
                        {
                            title: 'Malformed action',
                            open_action: { kind: 'open_conversation', session_id: '' },
                        },
                        {
                            title: 'Unsupported action',
                            open_action: { kind: 'external_url', href: 'javascript:alert(1)' },
                        },
                    ],
                    more_count: 2,
                },
            },
        });

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');
        await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#project',
            suffix: 'test',
        });

        expect(document.querySelectorAll('.concept-summary-conversation-card')).toHaveLength(3);
        expect(Array.from(document.querySelectorAll('.concept-summary-conversation-title')).map(
            (title) => title.textContent,
        )).toEqual(['Useful discussion', 'Malformed action', 'Unsupported action']);
        expect(document.querySelectorAll('.concept-summary-open-conversation')).toHaveLength(1);
        expect(document.querySelector('.concept-summary-conversations-more').textContent).toBe(
            '2 more in Conversations',
        );
        expect(document.querySelector('a')).toBeNull();
    });

    test('reports a failed conversation switch and re-enables the action', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const chatTab = require('../../src/frontend/web/von_interface/static/js/chatTab.js');
        const tabNavigation = require('../../src/frontend/web/von_interface/static/js/tabNavigation.js');
        chatTab.switchToChatSession.mockResolvedValue({ ok: false, error: 'revoked' });
        api.getJsonDetailed.mockResolvedValue({
            data: {
                panel: {
                    variant: 'conversation',
                    title: 'Revoked conversation',
                    open_action: {
                        kind: 'open_conversation',
                        session_id: 'session-revoked',
                    },
                },
            },
        });

        const {
            ensureConceptSummaryRendererPanelForConceptTab,
        } = require('../../src/frontend/web/von_interface/static/js/components/conceptSummaryRendererPanel.js');
        await ensureConceptSummaryRendererPanelForConceptTab({
            conceptId: '#V#conversation_revoked',
            suffix: 'test',
        });

        const button = document.querySelector('.concept-summary-open-conversation');
        button.click();
        await flushMicrotasks();

        expect(button.disabled).toBe(false);
        expect(button.textContent).toBe('Open conversation');
        expect(document.querySelector('.concept-summary-action-status').textContent).toBe(
            'Could not open this conversation. Please try again.',
        );
        expect(tabNavigation.activateTab).not.toHaveBeenCalled();
    });
});
