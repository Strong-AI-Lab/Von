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

describe('chat conversation LLM telemetry copy control', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <button id="copyConversationLlmDebugJsonBtn"
                data-copy-json-role="copy-json"
                title="Copy conversation-level LLM telemetry JSON"
                aria-label="Copy conversation LLM telemetry as JSON">LLM(i)</button>
        `;

        const clipboardWriteText = jest.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            writable: true,
            value: { writeText: clipboardWriteText }
        });
    });

    test('disables copy button and reports info when no telemetry exists', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyConversationLlmDebugJsonBtn');

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.__testOnly_refreshConversationLlmCopyButtonState();

        expect(button.disabled).toBe(true);
        expect(button.getAttribute('aria-disabled')).toBe('true');
        expect(button.title).toContain('not available');

        const copied = await chatTab.__testOnly_copyConversationLlmTelemetryToClipboard(button);
        expect(copied).toBe(false);
        expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
        expect(showToast).toHaveBeenCalledWith(
            'No conversation-level LLM telemetry is available yet.',
            'info'
        );
    });

    test('copies deterministic conversation-level LLM telemetry JSON', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyConversationLlmDebugJsonBtn');

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-200', {
            timestamp: '2026-03-03T00:00:02.000Z',
            model: 'gpt-test',
            tool_invocations: [{ method: 'search_knowledge_base', success: true }]
        });
        chatTab.setLlmDebugDataForTurn('a-100', {
            timestamp: '2026-03-03T00:00:01.000Z',
            model: 'gpt-test',
            response: 'Earlier turn'
        });

        chatTab.__testOnly_refreshConversationLlmCopyButtonState();
        expect(button.disabled).toBe(false);
        expect(button.getAttribute('aria-disabled')).toBe('false');

        const payload = chatTab.__testOnly_buildConversationLlmTelemetryPayload();
        expect(payload).toBeTruthy();
        expect(payload.schema_version).toBe('conversation_llm_telemetry.v1');
        expect(payload.metadata.llm_debug_turn_count).toBe(2);
        expect(payload.turns.map((turn) => turn.turn_id)).toEqual(['a-100', 'a-200']);

        const copied = await chatTab.__testOnly_copyConversationLlmTelemetryToClipboard(button);
        expect(copied).toBe(true);
        expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);

        const copiedText = navigator.clipboard.writeText.mock.calls[0][0];
        const copiedPayload = JSON.parse(copiedText);
        expect(copiedPayload.schema_version).toBe('conversation_llm_telemetry_locator.v1');
        expect(copiedPayload.turns).toHaveLength(2);
        expect(copiedPayload.turns.map((turn) => turn.turn_id)).toEqual(['a-100', 'a-200']);
        expect(copiedPayload.mcp_access).toEqual(expect.objectContaining({
            conversation_telemetry_get_locator: expect.any(Object),
            chat_history_get_segments: expect.any(Object),
            turn_execution_list: expect.any(Object)
        }));
        expect(showToast).toHaveBeenCalledWith(
            'Copied conversation telemetry locator JSON (partial coverage).',
            'info'
        );
    });
});
