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
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

describe('thinking card selector telemetry rendering', () => {
    test('renders selector prompt and candidate list for workflow dispatch diagnostics', () => {
        const { __testOnly_renderThinkingCardBodyHTML } = require(chatTabModulePath);

        const html = __testOnly_renderThinkingCardBodyHTML({
            thinkingCardMode: 'debug',
            latestProgress: {
                phase: 'workflow_dispatch',
                stage: 'workflow_dispatch',
                result_summary: 'Answering directly from accessible context'
            },
            workflowStagePath: {
                path: [
                    {
                        stage_id: 'workflow_dispatch_prepare',
                        stage_label: 'Workflow dispatch preparation'
                    },
                    {
                        stage_id: 'workflow_dispatch',
                        stage_label: 'Workflow dispatch'
                    }
                ]
            },
            stageDiagnostics: [
                {
                    stage_id: 'workflow_dispatch_prepare',
                    event_count: 1,
                    latest_result_summary: 'Preparing workflow dispatch'
                },
                {
                    stage_id: 'workflow_dispatch',
                    event_count: 1,
                    latest_result_summary: 'Answering directly from accessible context'
                }
            ],
            workflowDiscovery: {
                candidate_count: 1,
                match_count: 1,
                candidates: [
                    {
                        concept_id: '#V#concept_search_instance_retrieval_workflow',
                        name: 'Concept Search Instance Retrieval Workflow'
                    }
                ],
                matches: [
                    {
                        concept_id: '#V#concept_search_instance_retrieval_workflow',
                        name: 'Concept Search Instance Retrieval Workflow'
                    }
                ]
            },
            workflowRoutingDiagnostics: {
                schema_version: 'workflow_routing_diagnostics.v1',
                selected_workflow_id: '#V#concept_search_instance_retrieval_workflow',
                selector_verdict: 'rag_selected',
                selector_source: 'selector',
                selector: {
                    prompt_id: '#V#chat_turn_classifier_prompt',
                    requested_prompt_ids: ['#V#chat_turn_classifier_prompt'],
                    prompt: {
                        text: 'Current user request:\nTell me about myself.\nCandidate workflows:\n- #V#concept_search_instance_retrieval_workflow',
                        char_count: 109
                    },
                    candidate_list: {
                        text: '- #V#concept_search_instance_retrieval_workflow: Concept Search Instance Retrieval Workflow',
                        char_count: 93
                    },
                    response: {
                        text: '{"workflow_id":"#V#concept_search_instance_retrieval_workflow"}',
                        char_count: 65
                    }
                }
            }
        });

        expect(html).toContain('Selector prompt');
        expect(html).toContain('Tell me about myself.');
        expect(html).toContain('Selector candidate list');
        expect(html).toContain('#V#concept_search_instance_retrieval_workflow');
        expect(html).toContain('Requested selector prompt ids');
        expect(html).toContain('#V#chat_turn_classifier_prompt');
        expect(html).toContain('Selector response');
    });

    test('keeps expanded live LLM interaction details when a refresh omits live events', () => {
        const { __testOnly_renderThinkingCardBodyHTML } = require(chatTabModulePath);

        const request = {
            clientRequestId: 'req-llm-expanded-stability',
            latestProgress: {
                request_id: 'req-llm-expanded-stability',
                status: 'running',
                stage: 'selector_preparation',
                phase: 'selector_preparation',
                diagnostic_events: [
                    {
                        sequence_no: 84,
                        workflow_stage_id: 'selector_preparation',
                        model: 'gpt-oss:20b',
                        provider: 'ollama',
                        llm_request_state: 'completed',
                        llm_request_sent_at_utc: '2026-06-03T15:02:47.641Z',
                        at_utc: '2026-06-03T15:03:51.917Z',
                        duration_ms: 60557,
                        prompt_preview: {
                            text: 'Select workflow for this user request: Look in recent email for arXiv papers.'
                        },
                        llm_response_preview: {
                            text: '{"workflow_id":"#V#zhan_gmail_arxiv_ingestion_workflow"}'
                        }
                    }
                ]
            }
        };

        const firstHtml = __testOnly_renderThinkingCardBodyHTML(request);
        const fixture = document.createElement('div');
        fixture.innerHTML = firstHtml;
        const row = fixture.querySelector('details[data-thinking-llm-call-log-key]');

        expect(row).not.toBeNull();
        expect(firstHtml).toContain('Select workflow for this user request');

        request.expandedThinkingDiagnosticKeys = new Set([row.dataset.thinkingDiagnosticKey]);
        request.latestProgress = {
            ...request.latestProgress,
            diagnostic_events: []
        };

        const refreshedHtml = __testOnly_renderThinkingCardBodyHTML(request);

        expect(refreshedHtml).toContain('Keeping previously expanded LLM exchange details visible');
        expect(refreshedHtml).toContain('Select workflow for this user request');
        expect(refreshedHtml).toContain('#V#zhan_gmail_arxiv_ingestion_workflow');
        expect(refreshedHtml).not.toContain('No live LLM events have arrived yet');
    });
});
