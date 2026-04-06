/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn()
}));

describe('LLM debug popup workflow execution hook', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="chatLlmDebugPopup" class="hidden" aria-hidden="true"></div>
            <div id="chatLlmDebugMeta"></div>
            <pre id="chatLlmDebugMessages"></pre>
            <pre id="chatLlmDebugResponse"></pre>

            <div id="chatLlmDebugToolsSection" class="hidden"></div>
            <pre id="chatLlmDebugTools"></pre>

            <div id="chatLlmDebugAuxSection" class="hidden"></div>
            <pre id="chatLlmDebugAux"></pre>

            <button id="closeChatLlmDebug"></button>
        `;
    });

    test('surfaces execution_id and links to trace endpoint', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        const turnId = 'assistant-123';
        const executionId = 'exec-abc/123';

        setLlmDebugDataForTurn(turnId, {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            aux_llm_calls: [
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#example_workflow',
                    execution_id: executionId,
                    stored: true,
                    status: 'stored'
                }
            ]
        });

        await showLlmDebugPopup(turnId);

        const metaDiv = document.getElementById('chatLlmDebugMeta');
        expect(metaDiv.innerHTML).toContain('Workflow execution');
        expect(metaDiv.innerHTML).toContain('exec-abc/123');

        const link = metaDiv.querySelector('a');
        expect(link).not.toBeNull();
        expect(link.getAttribute('href')).toBe(`/api/workflows/executions/${encodeURIComponent(executionId)}`);

        const popup = document.getElementById('chatLlmDebugPopup');
        expect(popup.classList.contains('hidden')).toBe(false);
        expect(popup.getAttribute('aria-hidden')).toBe('false');

        const auxSection = document.getElementById('chatLlmDebugAuxSection');
        expect(auxSection.classList.contains('hidden')).toBe(false);
    });

    test('prefers turn_execution_diagnostics for popup copy payload', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        const turnId = 'assistant-456';
        setLlmDebugDataForTurn(turnId, {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            workflow_discovery: { matches: [{ concept_id: '#V#demo' }] },
            turn_execution_diagnostics: {
                generated_at_utc: '2026-02-18T00:00:00Z',
                request_id: 'req-456',
                elapsed_ms: 1234,
                prompt_preview: 'hello',
                latest_progress: { status: 'completed' },
                progress_events: [],
                phase_history: [],
                tool_history: [],
                stage_diagnostics: [
                    {
                        stage_id: 'workflow_discovery',
                        event_count: 1
                    }
                ]
            }
        });

        await showLlmDebugPopup(turnId);

        const popup = document.getElementById('chatLlmDebugPopup');
        const jsonText = popup.dataset.currentDebugData || '';
        const payload = JSON.parse(jsonText);

        expect(payload.schema_version).toBe('turn_telemetry_locator.v1');
        expect(payload.request_id).toBe('req-456');
        expect(payload.prompt_preview).toBe('hello');
        expect(payload.workflow_discovery).toEqual({ matches: [{ concept_id: '#V#demo' }] });
        expect(payload.stage_diagnostics).toEqual([
            expect.objectContaining({
                stage_id: 'workflow_discovery',
                event_count: 1
            })
        ]);
        expect(payload.mcp_access).toEqual(expect.objectContaining({
            turn_execution_get_diagnostics: expect.any(Object)
        }));
        expect(payload.model).toBeUndefined();
        expect(payload.messages).toBeUndefined();
    });

    test('hydrates popup locator history_location from the server conversation locator when missing locally', async () => {
        const {
            __testOnly_setActiveChatSession,
            setLlmDebugDataForTurn,
            showLlmDebugPopup
        } = require(chatTabModulePath);
        const { getCurrentUserConceptId } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');

        getCurrentUserConceptId.mockReturnValue('#V#test_user');
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#test_user' }));
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#org' }));
        __testOnly_setActiveChatSession('session-1718', 'Session 1718');
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            json: async () => ({
                schema_version: 'conversation_llm_telemetry_locator.v1',
                session_id: 'session-1718',
                turns: [
                    {
                        turn_id: 'assistant-1718',
                        request_id: 'req-1718',
                        history_location: {
                            session_id: 'session-1718',
                            history_index: 17
                        }
                    }
                ]
            })
        });

        setLlmDebugDataForTurn('assistant-1718', {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            turn_execution_diagnostics: {
                request_id: 'req-1718',
                prompt_preview: 'Represent this paper'
            }
        });

        await showLlmDebugPopup('assistant-1718');

        const popup = document.getElementById('chatLlmDebugPopup');
        const payload = JSON.parse(popup.dataset.currentDebugData || '{}');

        const [calledUrl] = global.fetch.mock.calls[0];
        const parsedUrl = new URL(calledUrl, 'https://example.test');
        expect(parsedUrl.pathname).toBe('/von/history/telemetry_locator');
        expect(parsedUrl.searchParams.get('session_id')).toBe('session-1718');
        expect(parsedUrl.searchParams.get('namespace')).toBe('#V#test_user@org');
        expect(parsedUrl.searchParams.get('user_concept_id')).toBe('#V#test_user');
        expect(parsedUrl.searchParams.get('organisation_concept_id')).toBe('#V#org');
        expect(payload.history_location).toEqual({
            session_id: 'session-1718',
            history_index: 17
        });
        expect(payload.mcp_access).toEqual(expect.objectContaining({
            chat_history_get_debug_entry: expect.any(Object),
            conversation_telemetry_get_locator: expect.any(Object),
            turn_execution_get_diagnostics: expect.any(Object)
        }));
    });
});
