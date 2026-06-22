/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

function flushAsyncClickHandler() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
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
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

describe('LLM debug popup workflow execution hook', () => {
    beforeEach(() => {
        global.fetch = undefined;
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockReset();
        document.body.innerHTML = `
            <div id="scrollableField"></div>
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

    test('conversation turn LLM button copies locator JSON on plain click and marks copied', async () => {
        const {
            __testOnly_appendMessage,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        setLlmDebugDataForTurn('assistant-copy', {
            model: 'gpt-5.2-test',
            messages: [{ role: 'user', content: 'hello' }],
            response: 'ok',
            turn_execution_diagnostics: {
                request_id: 'req-copy',
                prompt_preview: 'hello'
            }
        });

        __testOnly_appendMessage('Von', 'Done', 'assistant-copy', true);

        const popup = document.getElementById('chatLlmDebugPopup');
        const button = document.querySelector('.llm-debug-button');
        expect(button).not.toBeNull();
        expect(button.classList.contains('llm-debug-button-copy-available')).toBe(true);

        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await Promise.resolve();
        await Promise.resolve();
        await flushAsyncClickHandler();

        expect(writeText).toHaveBeenCalledTimes(1);
        const copiedPayload = JSON.parse(writeText.mock.calls[0][0]);
        expect(copiedPayload.schema_version).toBe('turn_telemetry_locator.v1');
        expect(copiedPayload.request_id).toBe('req-copy');
        expect(copiedPayload.prompt_preview).toBe('hello');
        expect(copiedPayload.messages).toBeUndefined();
        expect(copiedPayload.response).toBeUndefined();
        expect(button.classList.contains('llm-debug-button-copied')).toBe(true);
        expect(button.textContent).toBe('Copied');
        expect(button.dataset.copyFeedback).toBe('Copied');
        expect(button.getAttribute('title')).toContain('Copied LLM reference JSON');
        expect(popup.classList.contains('hidden')).toBe(true);
    });

    test('conversation turn LLM button copies history locator without hydrating debug data', async () => {
        const {
            __testOnly_appendMessage,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });
        global.fetch = jest.fn();
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({
                schema_version: 'conversation_llm_telemetry_locator.v1',
                session_id: 'session-history-ref',
                turns: [
                    {
                        turn_id: 'history-assistant',
                        request_id: 'req-history-ref',
                        history_location: {
                            session_id: 'session-history-ref',
                            history_index: 4
                        }
                    }
                ]
            })
        });

        setLlmDebugDataForTurn('assistant-history-ref', {
            history_location: {
                session_id: 'session-history-ref',
                history_index: 4
            },
            timestamp: '2026-06-03T00:00:00.000Z'
        });

        __testOnly_appendMessage('Von', 'Stored history turn', 'assistant-history-ref', false, true);

        const button = document.querySelector('.llm-debug-button');
        expect(button).not.toBeNull();

        button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await Promise.resolve();
        await Promise.resolve();
        await flushAsyncClickHandler();

        expect(global.fetch).not.toHaveBeenCalled();
        const [calledUrl] = fetchWithTimeout.mock.calls[0];
        const parsedUrl = new URL(calledUrl, 'https://example.test');
        expect(parsedUrl.pathname).toBe('/von/history/telemetry_locator');
        expect(parsedUrl.searchParams.get('session_id')).toBe('session-history-ref');
        expect(writeText).toHaveBeenCalledTimes(1);
        const copiedPayload = JSON.parse(writeText.mock.calls[0][0]);
        expect(copiedPayload.schema_version).toBe('turn_telemetry_locator.v1');
        expect(copiedPayload.request_id).toBe('req-history-ref');
        expect(copiedPayload.history_location).toEqual({
            session_id: 'session-history-ref',
            history_index: 4
        });
        expect(copiedPayload.mcp_access).toEqual(expect.objectContaining({
            chat_history_get_debug_entry: expect.any(Object),
            turn_execution_get_diagnostics: expect.any(Object)
        }));
        expect(copiedPayload.llm_debug_data).toBeUndefined();
        expect(copiedPayload.messages).toBeUndefined();
        expect(copiedPayload.response).toBeUndefined();
        expect(copiedPayload.workflow_discovery).toBeUndefined();
        expect(copiedPayload.stage_diagnostics).toBeUndefined();
    });

    test('conversation turn LLM button keeps popup behaviour on shift-click', async () => {
        const {
            __testOnly_appendMessage,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        setLlmDebugDataForTurn('assistant-popup', {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            turn_execution_diagnostics: {
                request_id: 'req-popup',
                prompt_preview: 'open details'
            }
        });

        __testOnly_appendMessage('Von', 'Done', 'assistant-popup', true);

        const popup = document.getElementById('chatLlmDebugPopup');
        const button = document.querySelector('.llm-debug-button');
        button.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));
        await Promise.resolve();
        await Promise.resolve();
        await flushAsyncClickHandler();

        expect(writeText).not.toHaveBeenCalled();
        expect(popup.classList.contains('hidden')).toBe(false);
        expect(popup.getAttribute('aria-hidden')).toBe('false');
        const payload = JSON.parse(popup.dataset.currentDebugData || '{}');
        expect(payload.request_id).toBe('req-popup');
    });

    test('surfaces execution traces and prefers the selected-workflow trace', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        const turnId = 'assistant-123';
        const selectedExecutionId = 'exec-selected/123';
        const auxiliaryExecutionId = 'exec-aux/456';

        setLlmDebugDataForTurn(turnId, {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            turn_execution_diagnostics: {
                workflow_selection: {
                    selected_workflow_id: '#V#example_workflow'
                }
            },
            aux_llm_calls: [
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#chat_narration_workflow',
                    execution_id: auxiliaryExecutionId,
                    stored: true,
                    status: 'stored'
                },
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#example_workflow',
                    execution_id: selectedExecutionId,
                    stored: true,
                    status: 'stored'
                }
            ]
        });

        await showLlmDebugPopup(turnId);

        const metaDiv = document.getElementById('chatLlmDebugMeta');
        expect(metaDiv.innerHTML).toContain('Workflow execution traces');
        expect(metaDiv.innerHTML).toContain('exec-selected/123');
        expect(metaDiv.innerHTML).toContain('selected workflow');
        expect(metaDiv.innerHTML).toContain('exec-aux/456');

        const link = metaDiv.querySelector('a');
        expect(link).not.toBeNull();
        expect(link.getAttribute('href')).toBe(`/api/workflows/executions/${encodeURIComponent(selectedExecutionId)}`);

        const popup = document.getElementById('chatLlmDebugPopup');
        expect(popup.classList.contains('hidden')).toBe(false);
        expect(popup.getAttribute('aria-hidden')).toBe('false');

        const auxSection = document.getElementById('chatLlmDebugAuxSection');
        expect(auxSection.classList.contains('hidden')).toBe(false);
    });

    test('prefers turn_execution_diagnostics for popup copy payload and preserves selector telemetry', async () => {
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
                workflow_routing_diagnostics: {
                    schema_version: 'workflow_routing_diagnostics.v1',
                    selected_workflow_id: '#V#concept_search_instance_retrieval_workflow',
                    selector_verdict: 'rag_selected',
                    selector_source: 'selector',
                    selector: {
                        prompt_id: '#V#chat_turn_classifier_prompt',
                        requested_prompt_ids: ['#V#chat_turn_classifier_prompt'],
                        prompt: {
                            text: 'Current user request:\nTell me about myself.',
                            char_count: 42
                        },
                        candidate_list: {
                            text: '- #V#concept_search_instance_retrieval_workflow',
                            char_count: 47
                        },
                        response: {
                            text: '{"workflow_id":"#V#concept_search_instance_retrieval_workflow"}',
                            char_count: 65
                        }
                    }
                },
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
        expect(payload.workflow_discovery).toBeUndefined();
        expect(payload.stage_diagnostics).toBeUndefined();
        expect(payload.diagnostic_summary).toEqual(expect.objectContaining({
            has_hydrated_debug_payload: true,
            workflow_discovery_available: true,
            workflow_discovery_candidate_count: 1,
            stage_diagnostics_count: 1
        }));
        expect(payload.workflow_routing_diagnostics).toEqual({
            schema_version: 'workflow_routing_diagnostics.v1',
            selected_workflow_id: '#V#concept_search_instance_retrieval_workflow',
            selector_verdict: 'rag_selected',
            selector_source: 'selector',
            selector: {
                prompt_id: '#V#chat_turn_classifier_prompt',
                requested_prompt_ids: ['#V#chat_turn_classifier_prompt'],
                prompt_provenance: null,
                prompt: {
                    preview: 'Current user request:\nTell me about myself.',
                    char_count: 42,
                    preview_truncated: false
                },
                candidate_list: {
                    preview: '- #V#concept_search_instance_retrieval_workflow',
                    char_count: 47,
                    preview_truncated: false
                },
                response: {
                    preview: '{"workflow_id":"#V#concept_search_instance_retrieval_workflow"}',
                    char_count: 65,
                    preview_truncated: false
                }
            }
        });
        expect(payload.mcp_access).toEqual(expect.objectContaining({
            turn_execution_get_diagnostics: expect.any(Object)
        }));
        expect(payload.model).toBeUndefined();
        expect(payload.messages).toBeUndefined();
    });

    test('copy payload keeps auxiliary traces separate from the selected-workflow trace', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        setLlmDebugDataForTurn('assistant-789', {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            turn_execution_diagnostics: {
                request_id: 'req-789',
                workflow_selection: {
                    selected_workflow_id: '#V#arxiv_paper_representation_workflow'
                }
            },
            aux_llm_calls: [
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#chat_narration_workflow',
                    execution_id: 'exec-narration',
                    stored: true,
                    status: 'stored'
                },
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#arxiv_paper_representation_workflow',
                    execution_id: 'exec-selected',
                    stored: true,
                    status: 'stored'
                }
            ]
        });

        await showLlmDebugPopup('assistant-789');

        const popup = document.getElementById('chatLlmDebugPopup');
        const payload = JSON.parse(popup.dataset.currentDebugData || '{}');

        expect(payload.mcp_access.workflow_get_execution_trace).toEqual(expect.objectContaining({
            tool_name: 'workflow_get_execution_trace',
            arguments: expect.objectContaining({
                execution_id: 'exec-selected'
            })
        }));
        expect(payload.mcp_access.workflow_execution_traces).toEqual([
            expect.objectContaining({
                workflow_id: '#V#chat_narration_workflow',
                trace_role: 'auxiliary_workflow'
            }),
            expect.objectContaining({
                workflow_id: '#V#arxiv_paper_representation_workflow',
                trace_role: 'selected_workflow'
            })
        ]);
    });

    test('copy payload treats workflow-use episode instance as selected-workflow evidence', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        setLlmDebugDataForTurn('assistant-790', {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'failed',
            workflow_routing: {
                workflow_id: '#V#arxiv_paper_representation_workflow',
                verdict: 'rag_selected'
            },
            aux_llm_calls: [
                {
                    type: 'workflow_execution_trace',
                    workflow_id: '#V#chat_narration_workflow',
                    execution_id: 'exec-narration',
                    stored: true,
                    status: 'stored'
                },
                {
                    type: 'workflow_use_episode',
                    workflow_id: '#V#arxiv_paper_representation_workflow',
                    workflow_instance_id: 'wf-instance-selected',
                    completed: false
                }
            ]
        });

        await showLlmDebugPopup('assistant-790');

        const popup = document.getElementById('chatLlmDebugPopup');
        const payload = JSON.parse(popup.dataset.currentDebugData || '{}');

        expect(payload.mcp_access.workflow_get_execution_trace).toEqual(expect.objectContaining({
            tool_name: 'workflow_get_execution_trace',
            arguments: expect.objectContaining({
                instance_id: 'wf-instance-selected'
            })
        }));
        expect(payload.mcp_access.workflow_execution_traces).toEqual([
            expect.objectContaining({
                workflow_id: '#V#chat_narration_workflow',
                trace_role: 'auxiliary_workflow'
            }),
            expect.objectContaining({
                workflow_id: '#V#arxiv_paper_representation_workflow',
                instance_id: 'wf-instance-selected',
                trace_role: 'selected_workflow'
            })
        ]);
    });

    test('hydrates popup locator history_location from the server conversation locator when missing locally', async () => {
        const {
            __testOnly_setActiveChatSession,
            setLlmDebugDataForTurn,
            showLlmDebugPopup
        } = require(chatTabModulePath);
        const { getCurrentUserConceptId } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');

        getCurrentUserConceptId.mockReturnValue('#V#test_user');
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#test_user' }));
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#org' }));
        __testOnly_setActiveChatSession('session-1718', 'Session 1718');
        global.fetch = jest.fn();
        fetchWithTimeout.mockResolvedValue({
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

        const [calledUrl] = fetchWithTimeout.mock.calls[0];
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
