/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

function flushAsyncClickHandler() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

function buildFailureCapsule(overrides = {}) {
    return {
        schema_version: 'turn_failure_capsule.v1',
        generated_at_utc: '2026-08-17T10:40:58.471Z',
        request_id: 'req-capsule',
        terminal_status: 'effect_partially_completed',
        response_authority: 'canonical_outcome',
        producer: {
            code_version: '2026.08.17',
            git_commit: '0123456789abcdef'
        },
        visible_response: {
            text: 'The paper could not be downloaded, but the concept was represented.',
            char_count: 67,
            sha256: 'a'.repeat(64),
            truncated: false
        },
        pre_presentation_draft: {
            text: 'I represented the concept and attempted the paper download.',
            char_count: 58,
            sha256: 'b'.repeat(64),
            truncated: false,
            authority: 'non_authoritative'
        },
        effects: [
            {
                effect_id: 'effect-download-paper',
                name: 'download_paper',
                initial_status: 'indeterminate',
                status: 'failed',
                changed: false,
                current_outcome_status: 'failed',
                outcome_resolved: true,
                reconciliation_status: 'resolved',
                canonical_readback_verdict: 'not_present',
                error: {
                    code: 'arxiv_acquisition_unavailable',
                    preview: {
                        text: 'RuntimeError: asyncio lock is bound to a different event loop',
                        char_count: 65,
                        sha256: 'c'.repeat(64),
                        truncated: false
                    }
                },
                recovered_by_effect_id: 'effect-represent-concept',
                workflow_id: 'workflow-paper-representation',
                instance_id: 'instance-paper-representation',
                evidence_id: 'evidence-paper-download'
            },
            {
                effect_id: 'effect-represent-concept',
                name: 'represent_concept',
                initial_status: 'completed',
                status: 'completed',
                changed: true,
                current_outcome_status: 'completed',
                outcome_resolved: true,
                reconciliation_status: 'resolved',
                canonical_readback_verdict: 'verified'
            }
        ],
        effect_summary: {
            total_count: 2,
            included_count: 2,
            omitted_count: 0
        },
        canonical_scope_modes: ['user'],
        redaction: {
            applied: false,
            redacted_count: 0,
            truncated: false
        },
        ...overrides
    };
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
        window.scrollTo = jest.fn();
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

    test('plain click copies the motivating incident capsule without delegation or raw diagnostics', async () => {
        const {
            __testOnly_appendMessage,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({
                ...buildFailureCapsule({ request_id: 'req-copy' }),
                prompt_preview: 'DO_NOT_COPY_PROMPT_SENTINEL',
                messages: [{ role: 'user', content: 'DO_NOT_COPY_MESSAGE_SENTINEL' }],
                response: 'DO_NOT_COPY_RAW_RESPONSE_SENTINEL',
                namespace_context: {
                    namespace: '#V#private-user@private-org',
                    user_id: '#V#private-user',
                    org_id: '#V#private-org'
                },
                metadata: { secret: 'DO_NOT_COPY_METADATA_SENTINEL' },
                workflow_routing_diagnostics: { raw: 'DO_NOT_COPY_ROUTING_SENTINEL' },
                mcp_access: {
                    turn_execution_get_diagnostics: {
                        arguments: {
                            turn_telemetry_ref: {
                                signature: 'DO_NOT_COPY_SIGNATURE_SENTINEL',
                                nonce: 'DO_NOT_COPY_NONCE_SENTINEL'
                            }
                        }
                    }
                }
            })
        });

        setLlmDebugDataForTurn('assistant-copy', {
            model: 'gpt-5.2-test',
            messages: [{ role: 'user', content: 'hello' }],
            response: 'ok',
            turn_execution_diagnostics: {
                request_id: 'req-copy',
                prompt_preview: 'hello',
                history_location: {
                    session_id: 'session-copy',
                    history_index: 289
                }
            },
            error: 'DO_NOT_COPY_DEBUG_ERROR_SENTINEL'
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
        const copiedText = writeText.mock.calls[0][0];
        const copiedPayload = JSON.parse(copiedText);
        expect(Buffer.byteLength(copiedText, 'utf8')).toBeLessThanOrEqual(16 * 1024);
        expect(copiedPayload.schema_version).toBe('turn_failure_capsule.v1');
        expect(copiedPayload.request_id).toBe('req-copy');
        expect(copiedPayload.terminal_status).toBe('effect_partially_completed');
        expect(copiedPayload.response_authority).toBe('canonical_outcome');
        expect(copiedPayload.effects[0]).toEqual(expect.objectContaining({
            name: 'download_paper',
            status: 'failed',
            error: expect.objectContaining({
                code: 'arxiv_acquisition_unavailable',
                preview: expect.objectContaining({
                    text: 'RuntimeError: asyncio lock is bound to a different event loop'
                })
            })
        }));
        const [capsuleUrl] = fetchWithTimeout.mock.calls[0];
        const parsedUrl = new URL(capsuleUrl, 'https://example.test');
        expect(parsedUrl.pathname).toBe('/von/history/turn_failure_capsule');
        expect(parsedUrl.searchParams.get('request_id')).toBe('req-copy');
        expect(parsedUrl.searchParams.get('session_id')).toBe('session-copy');
        expect(parsedUrl.searchParams.get('history_index')).toBe('289');
        expect(fetchWithTimeout.mock.calls.some(([url]) => (
            String(url).includes('/turn_telemetry_access')
            || String(url).includes('/telemetry_locator')
        ))).toBe(false);
        expect(copiedText).not.toContain('DO_NOT_COPY_');
        expect(copiedPayload.prompt_preview).toBeUndefined();
        expect(copiedPayload.namespace_context).toBeUndefined();
        expect(copiedPayload.metadata).toBeUndefined();
        expect(copiedPayload.workflow_routing_diagnostics).toBeUndefined();
        expect(copiedPayload.mcp_access).toBeUndefined();
        expect(copiedPayload.messages).toBeUndefined();
        expect(copiedPayload.response).toBeUndefined();
        expect(button.classList.contains('llm-debug-button-copied')).toBe(true);
        expect(button.textContent).toBe('Copied');
        expect(button.dataset.copyFeedback).toBe('Copied');
        expect(button.getAttribute('title')).toContain('Copied bounded turn capsule JSON');
        expect(popup.classList.contains('hidden')).toBe(true);
    });

    test('fetched capsule is retained for historical copy while every fresh or expired credential is excluded', async () => {
        const {
            __testOnly_buildLlmDebugClipboardJsonForTurn,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockResolvedValueOnce({
            ok: true,
            json: async () => ({
                ...buildFailureCapsule({ request_id: 'req-history-ref' }),
                mcp_access: {
                    turn_execution_get_diagnostics: {
                        tool_name: 'turn_execution_get_diagnostics',
                        arguments: {
                            request_id: 'req-history-ref',
                            turn_telemetry_ref: {
                                signature: 'fresh-signature-sentinel',
                                nonce: 'fresh-nonce-sentinel',
                                expires_at_utc: '2099-01-01T00:00:00Z'
                            }
                        }
                    },
                    expired_descriptor: {
                        tool_name: 'turn_execution_get_diagnostics',
                        arguments: {
                            turn_telemetry_ref: {
                                signature: 'expired-signature-sentinel',
                                expires_at_utc: '2000-01-01T00:00:00Z'
                            }
                        }
                    }
                }
            })
        }).mockResolvedValueOnce({
            ok: false,
            json: async () => ({ error: 'failure_capsule_temporarily_unavailable' })
        });

        setLlmDebugDataForTurn('history-assistant', {
            request_id: 'req-history-ref',
            history_location: {
                session_id: 'session-history-ref',
                history_index: 4
            },
            timestamp: '2026-06-03T00:00:00.000Z'
        });

        const firstText = await __testOnly_buildLlmDebugClipboardJsonForTurn('history-assistant');
        const secondText = await __testOnly_buildLlmDebugClipboardJsonForTurn('history-assistant');

        expect(secondText).toBe(firstText);
        expect(fetchWithTimeout).toHaveBeenCalledTimes(2);
        expect(firstText).not.toContain('signature-sentinel');
        expect(firstText).not.toContain('nonce-sentinel');
        expect(firstText).not.toContain('expires_at_utc');
        expect(JSON.parse(firstText).mcp_access).toBeUndefined();
    });

    test('capsule projection redacts credential-shaped text and enforces the pretty UTF-8 16 KiB ceiling', async () => {
        const {
            __testOnly_buildLlmDebugClipboardJsonForTurn,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const oversizedEffects = Array.from({ length: 8 }, (_unused, index) => ({
            effect_id: index === 0
                ? 'signature=effect-identifier-secret'
                : `effect-${index}-${'x'.repeat(220)}`,
            name: index === 0
                ? 'nonce=effect-name-secret'
                : `effect_${index}_${'n'.repeat(260)}`,
            initial_status: 'indeterminate',
            status: 'failed',
            changed: false,
            current_outcome_status: 'failed',
            outcome_resolved: true,
            reconciliation_status: 'resolved',
            canonical_readback_verdict: 'not_present',
            error: {
                code: index === 0 ? 'arxiv_acquisition_unavailable' : `failure_${index}`,
                preview: {
                    text: `${index === 0 ? 'Bearer super-secret-token sk-abcdefghijk signature=error-signature-secret nonce=error-nonce-secret ' : ''}${'🙂'.repeat(4000)}`,
                    char_count: 8000,
                    sha256: 'd'.repeat(64),
                    truncated: false
                }
            },
            workflow_id: index === 0
                ? 'mongodb://db-user:db-password@private-cluster.example'
                : `workflow-${'w'.repeat(220)}`,
            instance_id: `instance-${'i'.repeat(220)}`,
            evidence_id: `evidence-${'e'.repeat(220)}`,
            arbitrary_raw_payload: 'DO_NOT_COPY_EFFECT_RAW_SENTINEL'
        }));
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => buildFailureCapsule({
                request_id: 'req-unicode-bound',
                producer: {
                    code_version: 'signature=producer-signature-secret',
                    git_commit: 'nonce=producer-nonce-secret'
                },
                visible_response: {
                    text: `Bearer visible-secret mongodb://reader:uri-password@cluster.example/path?access_token=query-token-secret -----BEGIN PRIVATE KEY-----\npem-private-key-secret\n-----END PRIVATE KEY----- ${'🙂'.repeat(20000)}`,
                    char_count: 40022,
                    sha256: 'e'.repeat(64),
                    truncated: false
                },
                pre_presentation_draft: {
                    text: `sk-abcdefghijk ${'漢'.repeat(20000)}`,
                    char_count: 20015,
                    sha256: 'f'.repeat(64),
                    truncated: false,
                    authority: 'non_authoritative'
                },
                effects: oversizedEffects,
                effect_summary: {
                    total_count: oversizedEffects.length,
                    included_count: oversizedEffects.length,
                    omitted_count: 0
                }
            })
        });
        setLlmDebugDataForTurn('assistant-unicode-bound', {
            request_id: 'req-unicode-bound',
            history_location: {
                session_id: 'session-unicode-bound',
                history_index: 7
            }
        });

        const jsonText = await __testOnly_buildLlmDebugClipboardJsonForTurn(
            'assistant-unicode-bound'
        );
        const payload = JSON.parse(jsonText);

        expect(Buffer.byteLength(jsonText, 'utf8')).toBeLessThanOrEqual(16 * 1024);
        expect(payload.terminal_status).toBe('effect_partially_completed');
        expect(payload.effects[0].error.code).toBe('arxiv_acquisition_unavailable');
        const copiedErrorPreview = payload.effects[0].error.preview;
        expect(copiedErrorPreview.sha256).toBeUndefined();
        expect(copiedErrorPreview.char_count).toBe(
            Array.from(copiedErrorPreview.text).length
        );
        expect(jsonText).not.toContain('super-secret-token');
        expect(jsonText).not.toContain('sk-abcdefghijk');
        expect(jsonText).not.toContain('effect-identifier-secret');
        expect(jsonText).not.toContain('effect-name-secret');
        expect(jsonText).not.toContain('db-password');
        expect(jsonText).not.toContain('producer-signature-secret');
        expect(jsonText).not.toContain('producer-nonce-secret');
        expect(jsonText).not.toContain('error-signature-secret');
        expect(jsonText).not.toContain('error-nonce-secret');
        expect(jsonText).not.toContain('uri-password');
        expect(jsonText).not.toContain('query-token-secret');
        expect(jsonText).not.toContain('pem-private-key-secret');
        expect(jsonText).not.toContain('DO_NOT_COPY_EFFECT_RAW_SENTINEL');
        expect(payload.redaction).toEqual(expect.objectContaining({
            applied: true,
            truncated: true
        }));
    });

    test('malformed or mismatched capsule fails closed without copying an old locator', async () => {
        const {
            __testOnly_buildLlmDebugClipboardJsonForTurn,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({
                ...buildFailureCapsule({ request_id: 'different-request' }),
                schema_version: 'turn_failure_capsule.future'
            })
        });
        setLlmDebugDataForTurn('assistant-malformed-capsule', {
            request_id: 'req-malformed-capsule',
            history_location: {
                session_id: 'session-malformed-capsule',
                history_index: 11
            },
            prompt_preview: 'OLD_LOCATOR_PROMPT_SENTINEL',
            telemetry_locator_mcp_access: {
                turn_execution_get_diagnostics: {
                    arguments: {
                        turn_telemetry_ref: { signature: 'OLD_LOCATOR_SIGNATURE_SENTINEL' }
                    }
                }
            }
        });

        const jsonText = await __testOnly_buildLlmDebugClipboardJsonForTurn(
            'assistant-malformed-capsule'
        );

        expect(jsonText).toBeNull();
        expect(fetchWithTimeout).toHaveBeenCalledTimes(1);
        const [calledUrl] = fetchWithTimeout.mock.calls[0];
        expect(calledUrl).toContain('/von/history/turn_failure_capsule?');
        expect(calledUrl).not.toContain('/telemetry_locator');
        expect(calledUrl).not.toContain('/turn_telemetry_access');
    });

    test('completed and unresolved neighbour capsules do not fabricate failures', async () => {
        const {
            __testOnly_buildLlmDebugClipboardJsonForTurn,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout
            .mockResolvedValueOnce({
                ok: true,
                json: async () => buildFailureCapsule({
                    request_id: 'req-completed-neighbour',
                    terminal_status: 'completed',
                    response_authority: 'model_answer',
                    effects: [],
                    effect_summary: {
                        total_count: 0,
                        included_count: 0,
                        omitted_count: 0
                    },
                    canonical_scope_modes: []
                })
            })
            .mockResolvedValueOnce({
                ok: true,
                json: async () => buildFailureCapsule({
                    request_id: 'req-unresolved-neighbour',
                    terminal_status: 'effect_outcome_unknown',
                    effects: [{
                        effect_id: 'effect-unknown',
                        name: 'represent_concept',
                        status: 'indeterminate',
                        outcome_resolved: false
                    }],
                    effect_summary: {
                        total_count: 1,
                        included_count: 1,
                        omitted_count: 0
                    }
                })
            });
        setLlmDebugDataForTurn('assistant-completed-neighbour', {
            request_id: 'req-completed-neighbour',
            history_location: {
                session_id: 'session-neighbours',
                history_index: 1
            }
        });
        setLlmDebugDataForTurn('assistant-unresolved-neighbour', {
            request_id: 'req-unresolved-neighbour',
            history_location: {
                session_id: 'session-neighbours',
                history_index: 2
            }
        });

        const completed = JSON.parse(await __testOnly_buildLlmDebugClipboardJsonForTurn(
            'assistant-completed-neighbour'
        ));
        const unresolved = JSON.parse(await __testOnly_buildLlmDebugClipboardJsonForTurn(
            'assistant-unresolved-neighbour'
        ));

        expect(completed.effects).toEqual([]);
        expect(completed.turn_error).toBeUndefined();
        expect(unresolved.effects).toEqual([expect.objectContaining({
            status: 'indeterminate',
            outcome_resolved: false
        })]);
        expect(unresolved.effects[0].error).toBeUndefined();
        expect(unresolved.turn_error).toBeUndefined();
    });

    test('copies a stored generic turn error without locator context or delegated access', async () => {
        const {
            __testOnly_buildLlmDebugClipboardJsonForTurn,
            setLlmDebugDataForTurn
        } = require(chatTabModulePath);
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        setLlmDebugDataForTurn('assistant-generic-turn-error', {
            turn_failure_capsule: buildFailureCapsule({
                request_id: 'req-generic-turn-error',
                terminal_status: 'failed',
                response_authority: 'model',
                effects: [],
                effect_summary: {
                    total_count: 0,
                    included_count: 0,
                    omitted_count: 0
                },
                turn_error: {
                    code: 'worker_terminated',
                    preview: {
                        text: 'The worker stopped before completing the turn.',
                        char_count: 46,
                        sha256: '1'.repeat(64),
                        truncated: false
                    }
                },
                mcp_access: {
                    turn_execution_get_diagnostics: {
                        arguments: {
                            turn_telemetry_ref: {
                                signature: 'DO_NOT_COPY_STORED_SIGNATURE_SENTINEL'
                            }
                        }
                    }
                }
            })
        });

        const jsonText = await __testOnly_buildLlmDebugClipboardJsonForTurn(
            'assistant-generic-turn-error'
        );
        const payload = JSON.parse(jsonText);

        expect(fetchWithTimeout).not.toHaveBeenCalled();
        expect(payload.turn_error).toEqual(expect.objectContaining({
            code: 'worker_terminated',
            preview: expect.objectContaining({
                text: 'The worker stopped before completing the turn.'
            })
        }));
        expect(payload.effects).toEqual([]);
        expect(jsonText).not.toContain('DO_NOT_COPY_STORED_SIGNATURE_SENTINEL');
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
        expect(payload.schema_version).toBe('turn_telemetry_locator.v1');
        expect(payload.request_id).toBe('req-popup');
        expect(payload.prompt_preview).toBe('open details');
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
        expect(payload.mcp_access).toEqual({});
        expect(payload.retrieval_status).toBe(
            'server_delegation_unavailable'
        );
        expect(payload.model).toBeUndefined();
        expect(payload.messages).toBeUndefined();
    });

    test('locator falls back to hydrated top-level prompt and routing diagnostics', async () => {
        const { setLlmDebugDataForTurn, showLlmDebugPopup } = require(chatTabModulePath);

        setLlmDebugDataForTurn('assistant-top-level-diagnostics', {
            model: 'gpt-5.2-test',
            messages: [],
            response: 'ok',
            prompt_preview: 'Please ingest https://arxiv.org/abs/2406.15341 into Von.',
            workflow_routing_diagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#tool_calling_workflow',
                selector_verdict: 'selected',
                selector_source: 'selector',
                selector: {
                    prompt_id: '#V#chat_turn_classifier_prompt',
                    candidate_list: {
                        text: '- #V#tool_calling_workflow: Tool Calling Workflow',
                        char_count: 46
                    },
                    response: {
                        text: '{"workflow_id":"#V#tool_calling_workflow"}',
                        char_count: 43
                    }
                }
            },
            turn_execution_diagnostics: {
                generated_at_utc: '2026-07-01T16:08:05.572Z',
                request_id: 'req-top-level-diagnostics',
                history_location: {
                    session_id: 'session-top-level-diagnostics',
                    history_index: 8
                },
                workflow_discovery: {
                    matches: [
                        {
                            concept_id: '#V#source_neutral_paper_reference_ingestion_workflow'
                        }
                    ]
                }
            }
        });

        await showLlmDebugPopup('assistant-top-level-diagnostics');

        const popup = document.getElementById('chatLlmDebugPopup');
        const payload = JSON.parse(popup.dataset.currentDebugData || '{}');

        expect(payload.prompt_preview).toBe(
            'Please ingest https://arxiv.org/abs/2406.15341 into Von.'
        );
        expect(payload.workflow_routing_diagnostics).toEqual(expect.objectContaining({
            selected_workflow_id: '#V#tool_calling_workflow',
            selector_verdict: 'selected',
            selector_source: 'selector'
        }));
        expect(payload.workflow_routing_diagnostics.selector).toEqual(
            expect.objectContaining({
                prompt_id: '#V#chat_turn_classifier_prompt',
                candidate_list: expect.objectContaining({
                    preview: '- #V#tool_calling_workflow: Tool Calling Workflow'
                }),
                response: expect.objectContaining({
                    preview: '{"workflow_id":"#V#tool_calling_workflow"}'
                })
            })
        );
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

        expect(payload.mcp_access.workflow_get_execution_trace).toBeUndefined();
        expect(payload.mcp_access.workflow_execution_traces).toBeUndefined();
        expect(payload.retrieval_status).toBe(
            'server_delegation_unavailable'
        );
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

        expect(payload.mcp_access.workflow_get_execution_trace).toBeUndefined();
        expect(payload.mcp_access.workflow_execution_traces).toBeUndefined();
        expect(payload.retrieval_status).toBe(
            'server_delegation_unavailable'
        );
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
                        },
                        mcp_access: {
                            chat_history_get_debug_entry: {
                                tool_name: 'chat_history_get_debug_entry',
                                arguments: {
                                    history_location_ref: {
                                        signature: 'signed-debug-ref'
                                    }
                                }
                            },
                            conversation_telemetry_get_locator: {
                                tool_name: 'conversation_telemetry_get_locator',
                                arguments: {
                                    conversation_ref: {
                                        signature: 'signed-conversation-ref'
                                    }
                                }
                            },
                            turn_execution_get_diagnostics: {
                                tool_name: 'turn_execution_get_diagnostics',
                                arguments: {
                                    request_id: 'req-1718',
                                    turn_telemetry_ref: {
                                        signature: 'signed-turn-ref'
                                    }
                                }
                            }
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
        expect(payload.retrieval_status).toBe(
            'server_delegation_available'
        );
    });
});
