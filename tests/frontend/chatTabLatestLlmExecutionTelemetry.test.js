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

    test('publishes the actual executed model and preserves a recovered fallback failure', () => {
        const domUtils = require('../../src/frontend/web/von_interface/static/js/domUtils.js');
        domUtils.getCurrentUserConceptId.mockReturnValue('#V#telemetry-user');
        sessionStorage.setItem('von_current_org', JSON.stringify({
            concept_id: '#V#telemetry-org',
            name: 'Telemetry Org',
        }));
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
                        note: 'llm.generate failed; trying fallback',
                        status: 'failed',
                        success: false,
                        error: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.',
                        failure_kind: 'quota_exhausted'
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
                    stage: 'turn_answer',
                    fallback_used: true,
                    selected: {
                        provider: 'ollama',
                        model: 'granite3.3:2b',
                        model_resolved: 'granite3.3:2b'
                    },
                    errors: [
                        {
                            failure_kind: 'quota_exhausted',
                            error: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.'
                        }
                    ],
                    fallback_attempts: [
                        {
                            status: 'failed',
                            model: 'gpt-5.4-mini',
                            failure_kind: 'quota_exhausted',
                            error: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.'
                        },
                        {
                            status: 'succeeded',
                            model: 'granite3.3:2b'
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
            primary_failure_kind: null,
            primary_failure_reason: null,
            recovered_failure_kind: 'quota_exhausted',
            recovered_failure_reason: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.'
        }));
        expect(published.context_binding).toEqual(expect.objectContaining({
            schema_version: 'llm_execution_context_binding.v1',
            generation: 1,
            window_session_id: 'test-window-session',
            user_concept_id: '#V#telemetry-user',
            organisation_concept_id: '#V#telemetry-org',
            conversation_session_id: null,
            configuration: expect.objectContaining({
                provider: 'openai',
                model: 'gpt-5.4-mini',
            }),
        }));
        expect(seen.at(-1)).toEqual(expect.objectContaining({
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            fallback_used: true
        }));
    });

    test('invalidates prior-org telemetry when an org switch starts with no active conversation', () => {
        const chatTab = require(chatTabModulePath);
        const seen = [];
        document.addEventListener('von:latestLlmExecutionTelemetryUpdated', (event) => {
            seen.push(event.detail);
        });

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-210', {
            model: 'gemma4:latest',
            llm_interaction: {
                requested_model: 'ollama/gemma4:latest',
                calls: [{ model: 'gemma4:latest', provider: 'ollama' }],
            },
        });
        const first = chatTab.__testOnly_getLatestLlmExecutionTelemetry();
        expect(first?.context_binding?.generation).toBe(1);
        expect(first?.context_binding?.conversation_session_id).toBeNull();

        const current = chatTab.__testOnly_invalidateLlmExecutionContextForOrgSwitch();

        expect(current.generation).toBe(2);
        expect(current.conversation_session_id).toBeNull();
        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toBeNull();
        expect(seen.at(-1)).toBeNull();
    });

    test('refines a thin history binding when detailed telemetry reveals an older model', () => {
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        }));
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-220', {
            timestamp: '2026-07-14T09:00:00.000Z',
            history_location: { session_id: 'conversation-a', history_index: 4 },
        });
        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toBeNull();

        chatTab.setLlmDebugDataForTurn('a-220', {
            timestamp: '2026-07-14T09:00:00.000Z',
            history_location: { session_id: 'conversation-a', history_index: 4 },
            model: 'gemma4:latest',
            llm_interaction: {
                requested_model: 'ollama/gemma4:latest',
                calls: [{ model: 'gemma4:latest', provider: 'ollama' }],
            },
        });

        const detailed = chatTab.__testOnly_getLatestLlmExecutionTelemetry();
        expect(detailed?.context_binding).toEqual(expect.objectContaining({
            generation: 1,
            conversation_session_id: 'conversation-a',
            configuration: expect.objectContaining({
                provider: 'ollama',
                model: 'gemma4:latest',
            }),
        }));
    });

    test('does not let an earlier failed auxiliary stage poison a later successful model call', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-2001', {
            timestamp: '2026-07-14T08:37:28.000Z',
            model: 'gemma4:latest',
            llm_interaction: {
                requested_model: 'gemma4:latest',
                calls: [
                    {
                        type: 'llm.generate',
                        stage: 'missing_tool_call_classifier',
                        model: 'gpt-5.6-luna',
                        provider: 'openai',
                        status: 'failed',
                        success: false,
                        error: 'Classifier model unavailable.',
                        failure_kind: 'provider_unreachable'
                    },
                    {
                        type: 'llm.generate',
                        stage: 'turn_answer',
                        model: 'gemma4:latest',
                        provider: 'ollama'
                    }
                ]
            },
            aux_llm_calls: [
                {
                    type: 'workflow_model_policy_stage',
                    stage: 'missing_tool_call_classifier',
                    selected: null,
                    fallback_used: true,
                    errors: [{
                        failure_kind: 'provider_unreachable',
                        error: 'Classifier model unavailable.'
                    }],
                    fallback_attempts: [{
                        status: 'failed',
                        model: 'gpt-5.6-luna',
                        failure_kind: 'provider_unreachable',
                        error: 'Classifier model unavailable.'
                    }]
                },
                {
                    type: 'workflow_model_policy_stage',
                    stage: 'turn_answer',
                    selected: {
                        provider: 'ollama',
                        model: 'gemma4:latest',
                        model_resolved: 'gemma4:latest'
                    },
                    fallback_used: false,
                    errors: [],
                    fallback_attempts: [{
                        status: 'succeeded',
                        model: 'gemma4:latest'
                    }]
                }
            ]
        });

        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toEqual(expect.objectContaining({
            requested_model: 'gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            execution_succeeded: true,
            fallback_used: true,
            primary_failure_kind: null,
            primary_failure_reason: null,
            recovered_failure_kind: 'provider_unreachable',
            recovered_failure_reason: 'Classifier model unavailable.'
        }));
    });

    test('keeps an unrecovered terminal model failure as the primary failure', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-2002', {
            timestamp: '2026-07-14T08:38:28.000Z',
            model: 'gemma4:latest',
            llm_interaction: {
                requested_model: 'gemma4:latest',
                calls: [{
                    type: 'llm.generate',
                    stage: 'turn_answer',
                    model: 'gemma4:latest',
                    provider: 'ollama',
                    status: 'failed',
                    success: false,
                    error: 'Ollama request timed out.',
                    failure_kind: 'request_timeout'
                }]
            },
            aux_llm_calls: [{
                type: 'workflow_model_policy_stage',
                stage: 'turn_answer',
                selected: null,
                fallback_used: true,
                errors: [{
                    failure_kind: 'request_timeout',
                    error: 'Ollama request timed out.'
                }],
                fallback_attempts: [{
                    status: 'failed',
                    model: 'gemma4:latest',
                    failure_kind: 'request_timeout',
                    error: 'Ollama request timed out.'
                }]
            }]
        });

        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toEqual(expect.objectContaining({
            requested_model: 'gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            execution_succeeded: false,
            fallback_used: true,
            primary_failure_kind: 'request_timeout',
            primary_failure_reason: 'Ollama request timed out.',
            recovered_failure_kind: null,
            recovered_failure_reason: null
        }));
    });

    test('does not let an unrelated earlier successful policy stage mask a terminal raw-call failure', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-2003', {
            timestamp: '2026-07-14T08:39:28.000Z',
            model: 'gemma4:latest',
            llm_interaction: {
                requested_model: 'gemma4:latest',
                calls: [
                    {
                        type: 'llm.generate',
                        stage: 'missing_tool_call_classifier',
                        model: 'gpt-5.6-luna',
                        provider: 'openai',
                        status: 'succeeded',
                        success: true
                    },
                    {
                        type: 'llm.generate',
                        stage: 'turn_answer',
                        model: 'gemma4:latest',
                        provider: 'ollama',
                        status: 'failed',
                        success: false,
                        error: 'The final Gemma request timed out.',
                        failure_kind: 'request_timeout'
                    }
                ]
            },
            aux_llm_calls: [{
                type: 'workflow_model_policy_stage',
                stage: 'missing_tool_call_classifier',
                selected: {
                    provider: 'openai',
                    model: 'gpt-5.6-luna',
                    model_resolved: 'gpt-5.6-luna'
                },
                fallback_used: false,
                errors: []
            }]
        });

        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toEqual(expect.objectContaining({
            requested_model: 'gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            execution_succeeded: false,
            primary_failure_kind: 'request_timeout',
            primary_failure_reason: 'The final Gemma request timed out.',
            recovered_failure_kind: null,
            recovered_failure_reason: null
        }));
    });

    test('publishes explicit stage override semantics from workflow policy telemetry', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-201', {
            timestamp: '2026-04-14T00:31:11.019Z',
            model: 'gpt-5.4-mini',
            llm_interaction: {
                requested_model: 'gpt-5.4-mini',
                calls: [
                    {
                        type: 'llm.generate',
                        model: 'gpt-4o-mini',
                        provider: 'openai',
                    }
                ]
            },
            aux_llm_calls: [
                {
                    type: 'workflow_model_policy_stage',
                    stage: 'workflow_dispatch',
                    policy_stage: 'classifier',
                    requested_model: 'gpt-5.4-mini',
                    selection_mode: 'policy_primary_override',
                    follows_active_llm: false,
                    explicit_stage_model_override: true,
                    explicit_stage_model_override_origin: 'policy_primary',
                    selected: {
                        provider: 'openai',
                        model: 'gpt-4o-mini',
                        model_resolved: 'gpt-4o-mini',
                        source: 'policy'
                    }
                }
            ]
        });

        const published = chatTab.__testOnly_getLatestLlmExecutionTelemetry();
        expect(published).toEqual(expect.objectContaining({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-4o-mini',
            actual_provider: 'openai',
            execution_stage: 'workflow_dispatch',
            policy_stage: 'classifier',
            selection_mode: 'policy_primary_override',
            follows_active_llm: false,
            explicit_stage_model_override: true,
            explicit_stage_model_override_origin: 'policy_primary'
        }));
    });

    test('does not promote presenter output health warnings to LLM execution failure', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-202', {
            timestamp: '2026-06-08T15:01:00.000Z',
            model: 'gpt-oss:20b',
            llm_interaction: {
                requested_model: 'ollama/gpt-oss:20b',
                calls: [
                    {
                        type: 'llm.generate',
                        model: 'gpt-oss:20b',
                        provider: 'ollama'
                    }
                ]
            },
            presenter_channels: {
                screen: 'On-screen answer',
                spoken: '',
                format: 'tagged_blocks_v1'
            },
            turn_output_health: {
                schema_version: 'turn_output_health_v1',
                status: 'degraded',
                issues: [
                    {
                        category: 'presenter_output',
                        code: 'missing_spoken_channel',
                        severity: 'warning',
                        message: 'Presenter output missing spoken channel; text-to-speech will fall back to screen text.',
                        fallback_used: 'screen_text_for_tts'
                    }
                ]
            }
        });

        const published = chatTab.__testOnly_getLatestLlmExecutionTelemetry();
        expect(published).toEqual(expect.objectContaining({
            requested_model: 'ollama/gpt-oss:20b',
            actual_model: 'gpt-oss:20b',
            actual_provider: 'ollama',
            primary_failure_reason: null,
            warnings: [
                'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
            ]
        }));
    });

    test('publishes bounded backup-credential transport telemetry', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-backup-key', {
            timestamp: '2026-09-03T15:00:00.000Z',
            model: 'gpt-5.4-mini',
            llm_interaction: {
                requested_model: 'gpt-5.4-mini',
                calls: [
                    {
                        type: 'llm.generate_with_tools',
                        model: 'gpt-5.4-mini',
                        provider: 'openai',
                        candidate: {
                            transport_metadata: {
                                credential_source: 'backup',
                                credential_failover_used: true,
                                primary_credential_failure_kind: 'quota_exhausted',
                            },
                        },
                    },
                ],
            },
        });

        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toEqual(
            expect.objectContaining({
                credential_source: 'backup',
                credential_failover_used: true,
                primary_credential_failure_kind: 'quota_exhausted',
                actual_model: 'gpt-5.4-mini',
                actual_provider: 'openai',
            }),
        );
    });

    test('publishes backup-credential telemetry from the live adaptive-turn call shape', () => {
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_clearLlmDebugData();
        chatTab.setLlmDebugDataForTurn('a-live-backup-key', {
            timestamp: '2026-09-03T15:26:53.031668Z',
            model: 'gpt-5.6-luna',
            llm_interaction: {
                requested_model: 'gpt-5.6-luna',
                calls: [
                    {
                        type: 'adaptive_turn_model_call',
                        model: 'gpt-5.6-luna',
                        provider: 'openai',
                        transport: {
                            credential_source: 'backup',
                            credential_failover_used: true,
                            primary_credential_failure_kind: 'quota_exhausted',
                        },
                    },
                ],
            },
        });

        expect(chatTab.__testOnly_getLatestLlmExecutionTelemetry()).toEqual(
            expect.objectContaining({
                credential_source: 'backup',
                credential_failover_used: true,
                primary_credential_failure_kind: 'quota_exhausted',
                actual_model: 'gpt-5.6-luna',
                actual_provider: 'openai',
            }),
        );
    });
});
