/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
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
    createVontologyAliasCartouche: jest.fn(),
    createVontologyCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    getCartoucheAppearanceSettings: jest.fn(() => ({
        useShortestName: false,
        showName: true,
        showId: true,
        showKind: true,
        kindAsBackground: false
    })),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptAlias: jest.fn(() => ''),
    normalisePotentialConceptId: jest.fn(() => ''),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn()
}));

describe('chat conversation info copy control', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <button id="copyConversationInfoJsonBtn"
                data-copy-json-role="copy-json"
                title="Copy conversation info as JSON"
                aria-label="Copy conversation info as JSON">ℹ</button>
        `;

        const clipboardWriteText = jest.fn().mockResolvedValue(undefined);
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            writable: true,
            value: { writeText: clipboardWriteText }
        });
        global.fetch = undefined;
    });

    test('disables copy button and reports info when no conversation exists', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyConversationInfoJsonBtn');

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.__testOnly_setTranscriptTurns([]);
        chatTab.__testOnly_setActiveChatSession(null, null);
        chatTab.__testOnly_refreshConversationInfoCopyButtonState();

        expect(button.disabled).toBe(true);
        expect(button.getAttribute('aria-disabled')).toBe('true');
        expect(button.title).toContain('not available');

        const copied = await chatTab.__testOnly_copyConversationInfoToClipboard(button);
        expect(copied).toBe(false);
        expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
        expect(showToast).toHaveBeenCalledWith(
            'No conversation info is available yet.',
            'info'
        );
    });

    test('copies session-level conversation info before turn telemetry exists', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyConversationInfoJsonBtn');

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.__testOnly_setTranscriptTurns([]);
        chatTab.__testOnly_setActiveChatSession('session-pre-first-response', 'Dino 2');
        chatTab.__testOnly_refreshConversationInfoCopyButtonState();

        expect(button.disabled).toBe(false);
        expect(button.getAttribute('aria-disabled')).toBe('false');
        expect(button.title).toBe('Copy conversation info as JSON');
        expect(button.getAttribute('aria-label')).toBe('Copy conversation info as JSON');

        const copied = await chatTab.__testOnly_copyConversationInfoToClipboard(button);
        expect(copied).toBe(true);
        expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);

        const copiedPayload = JSON.parse(navigator.clipboard.writeText.mock.calls[0][0]);
        expect(copiedPayload.schema_version).toBe('conversation_telemetry_access.v1');
        expect(copiedPayload.session_id).toBe('session-pre-first-response');
        expect(copiedPayload.session_name).toBe('Dino 2');
        expect(copiedPayload.metadata).toEqual(expect.objectContaining({
            total_turns: 0,
            llm_debug_turn_count: 0,
            transcript_turn_count: 0,
            authoritative_locator_available: false,
            access_payload_source: 'local_context_summary'
        }));
        expect(copiedPayload.mcp_access).toEqual({});
        expect(copiedPayload.retrieval_status).toBe(
            'server_delegation_unavailable'
        );
        expect(copiedPayload.turns).toBeUndefined();
        expect(copiedPayload.agent_instructions).toEqual(expect.objectContaining({
            summary: expect.any(String),
            steps: expect.any(Array),
            notes: expect.any(Array)
        }));
        expect(showToast).toHaveBeenCalledWith(
            'Copied conversation info JSON (server locator unavailable; local summary only).',
            'info'
        );
    });

    test('copies deterministic conversation info JSON when turn telemetry exists', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyConversationInfoJsonBtn');

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.__testOnly_setActiveChatSession('session-with-telemetry', 'Telemetry Session');
        chatTab.setLlmDebugDataForTurn('a-200', {
            timestamp: '2026-03-03T00:00:02.000Z',
            request_id: 'req-200',
            model: 'gpt-test',
            tool_invocations: [{ method: 'search_knowledge_base', success: true }]
        });
        chatTab.setLlmDebugDataForTurn('a-100', {
            timestamp: '2026-03-03T00:00:01.000Z',
            request_id: 'req-100',
            model: 'gpt-test',
            response: 'Earlier turn'
        });

        chatTab.__testOnly_refreshConversationInfoCopyButtonState();
        expect(button.disabled).toBe(false);
        expect(button.getAttribute('aria-disabled')).toBe('false');

        const payload = chatTab.__testOnly_buildConversationLlmTelemetryPayload();
        expect(payload).toBeTruthy();
        expect(payload.schema_version).toBe('conversation_llm_telemetry.v1');
        expect(payload.metadata.llm_debug_turn_count).toBe(2);
        expect(payload.turns.map((turn) => turn.turn_id)).toEqual(['a-100', 'a-200']);

        const copied = await chatTab.__testOnly_copyConversationInfoToClipboard(button);
        expect(copied).toBe(true);
        expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);

        const copiedText = navigator.clipboard.writeText.mock.calls[0][0];
        const copiedPayload = JSON.parse(copiedText);
        expect(copiedPayload.schema_version).toBe('conversation_telemetry_access.v1');
        expect(copiedPayload.session_id).toBe('session-with-telemetry');
        expect(copiedPayload.session_name).toBe('Telemetry Session');
        expect(copiedPayload.metadata).toEqual(expect.objectContaining({
            total_turns: 2,
            llm_debug_turn_count: 2,
            authoritative_locator_available: false,
            access_payload_source: 'local_context_summary'
        }));
        expect(copiedPayload.mcp_access).toEqual({});
        expect(copiedPayload.retrieval_status).toBe(
            'server_delegation_unavailable'
        );
        expect(copiedPayload.turns).toBeUndefined();
        expect(showToast).toHaveBeenCalledWith(
            'Copied conversation info JSON (server locator unavailable; local summary only).',
            'info'
        );
    });

    test('copies only server-issued conversation read delegations', async () => {
        const chatTab = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const button = document.getElementById('copyConversationInfoJsonBtn');
        const signedAccess = {
            conversation_telemetry_get_locator: {
                tool_name: 'conversation_telemetry_get_locator',
                arguments: {
                    conversation_ref: { signature: 'signed-conversation-ref' }
                }
            },
            chat_history_get_segments: {
                tool_name: 'chat_history_get_segments',
                arguments: {
                    conversation_ref: { signature: 'signed-segments-ref' }
                }
            }
        };

        global.fetch = jest.fn();
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({
                schema_version: 'conversation_llm_telemetry_locator.v1',
                generated_at_utc: '2026-07-28T12:00:00Z',
                session_id: 'session-signed',
                session_name: 'Signed Session',
                namespace_context: {},
                metadata: { total_turns: 0 },
                mcp_access: {
                    ...signedAccess,
                    turn_execution_list: {
                        tool_name: 'turn_execution_list',
                        arguments: {}
                    }
                },
                turns: []
            })
        });
        chatTab.__testOnly_setActiveChatSession('session-signed', 'Signed Session');

        const copied = await chatTab.__testOnly_copyConversationInfoToClipboard(button);

        expect(copied).toBe(true);
        const payload = JSON.parse(navigator.clipboard.writeText.mock.calls[0][0]);
        expect(payload.mcp_access).toEqual(signedAccess);
        expect(payload.mcp_access.turn_execution_list).toBeUndefined();
        expect(payload.retrieval_status).toBe('server_delegation_available');
    });
});
