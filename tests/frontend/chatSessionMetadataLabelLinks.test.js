/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

describe('chat session metadata label links', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="chatSessionMetadata"></div>';
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('Clicking group labels opens the corresponding type concept tab', () => {
        const { __test_only__renderChatSessionMetadataPanel } = require(chatTabModulePath);

        const seen = [];
        const handler = (e) => {
            seen.push(e?.detail);
        };
        document.addEventListener('von:selectConceptById', handler);

        __test_only__renderChatSessionMetadataPanel({
            sessionId: 's1',
            links: {},
            statusText: null,
            statusTone: null,
            disabled: false
        });

        const clickLabel = (labelText) => {
            const el = Array.from(document.querySelectorAll('.chat-session-metadata-label-link'))
                .find((node) => (node.textContent || '').trim() === labelText);
            expect(el).toBeTruthy();
            el.click();
        };

        clickLabel('Programmes');
        clickLabel('Projects');
        clickLabel('Activities');
        clickLabel('Modalities');

        expect(seen).toEqual([
            { conceptId: '#V#programme', createConceptTab: true },
            { conceptId: '#V#project', createConceptTab: true },
            { conceptId: '#V#work_activity', createConceptTab: true },
            { conceptId: '#V#conversation_modality', createConceptTab: true }
        ]);

        document.removeEventListener('von:selectConceptById', handler);
    });
});
