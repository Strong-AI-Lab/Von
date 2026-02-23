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

describe('chat diagnostics export shortcut', () => {
    test('recognises Ctrl+Shift+D as the export shortcut', () => {
        const { __testOnly_shouldHandleDiagnosticsExportShortcut } = require(chatTabModulePath);

        expect(__testOnly_shouldHandleDiagnosticsExportShortcut({
            key: 'd',
            ctrlKey: true,
            shiftKey: true,
            metaKey: false,
            altKey: false,
            defaultPrevented: false
        })).toBe(true);

        expect(__testOnly_shouldHandleDiagnosticsExportShortcut({
            key: 'd',
            ctrlKey: true,
            shiftKey: false,
            metaKey: false,
            altKey: false,
            defaultPrevented: false
        })).toBe(false);
    });

    test('sanitises tool arguments and omits message bodies in export payload', () => {
        const {
            setLlmDebugDataForTurn,
            __testOnly_buildDiagnosticsExportRequestPayload
        } = require(chatTabModulePath);

        setLlmDebugDataForTurn('a-9999999999999', {
            request_id: 'req-export-1',
            model: 'gpt-test',
            messages: [{ role: 'user', content: 'private content' }],
            tool_invocations: [
                {
                    method: 'search_knowledge_base',
                    arguments: {
                        query: 'sensitive query text',
                        namespace: '#V#test_user'
                    },
                    success: true
                }
            ],
            aux_llm_calls: [
                { type: 'workflow_discovery', model: 'gpt-test', duration_ms: 21 }
            ]
        });

        const payload = __testOnly_buildDiagnosticsExportRequestPayload();
        const latest = payload?.diagnostics?.latest_turn_debug;

        expect(payload.shortcut).toBe('Ctrl+Shift+D');
        expect(latest.type).toBe('llm_debug_summary');
        expect(latest.messages).toBeUndefined();
        expect(latest.tool_invocations).toEqual([
            expect.objectContaining({
                method: 'search_knowledge_base',
                argument_keys: ['query', 'namespace']
            })
        ]);
    });

    test('removes prompt preview when turn execution diagnostics are exported', () => {
        const { __testOnly_buildSanitisedLlmDebugExportPayload } = require(chatTabModulePath);

        const payload = __testOnly_buildSanitisedLlmDebugExportPayload({
            turn_execution_diagnostics: {
                request_id: 'req-turn-1',
                prompt_preview: 'should not be exported',
                elapsed_ms: 123
            }
        });

        expect(payload.type).toBe('turn_execution_diagnostics');
        expect(payload.request_id).toBe('req-turn-1');
        expect(payload.elapsed_ms).toBe(123);
        expect(payload.prompt_preview).toBeUndefined();
    });
});
