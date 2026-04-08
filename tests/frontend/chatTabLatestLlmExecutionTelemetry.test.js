/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(),
    renderSpanSuggestions: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyCartouche: jest.fn(),
    getCartoucheAppearanceSettings: jest.fn(() => ({
        useShortestName: false,
        showName: true,
        showId: true,
        showKind: true,
        kindAsBackground: false
    })),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptId: jest.fn(() => '')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn()
}));

describe('chatTab latest execution telemetry publication', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<button id="copyConversationInfoJsonBtn" data-copy-json-role="copy-json"></button>';
        localStorage.clear();
        sessionStorage.clear();
        window.__vonLatestLlmExecutionTelemetry = null;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
        window.__vonLatestLlmExecutionTelemetry = null;
    });

    test('publishes the actual executed model and fallback failure reason', () => {
        const chatTab = require(chatTabModulePath);
        const seen = [];
        document.addEventListener('von:latestLlmExecutionTelemetryUpdated', (event) => {
            seen.push(event.detail);
        });

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-200', {
            timestamp: '2026-04-08T20:39:11.019Z',
            model: 'gpt-5.4-mini',
            llm_interaction: {
                requested_model: 'gpt-5.4-mini',
                calls: [
                    {
                        type: 'llm.generate',
                        model: 'gpt-5.4-mini',
                        provider: 'openai',
                        note: 'llm.generate failed; trying fallback'
                    },
                    {
                        type: 'llm.generate',
                        model: 'granite3.3:2b',
                        provider: 'ollama'
                    }
                ]
            },
            aux_llm_calls: [
                {
                    type: 'workflow_model_policy_stage',
                    fallback_used: true,
                    errors: [
                        {
                            failure_kind: 'quota_exhausted',
                            error: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.'
                        }
                    ]
                }
            ]
        });

        const published = chatTab.__testOnly_getLatestLlmExecutionTelemetry();
        expect(published).toEqual(expect.objectContaining({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            fallback_used: true,
            primary_failure_kind: 'quota_exhausted',
            primary_failure_reason: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.'
        }));
        expect(seen.at(-1)).toEqual(expect.objectContaining({
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            fallback_used: true
        }));
    });
});
