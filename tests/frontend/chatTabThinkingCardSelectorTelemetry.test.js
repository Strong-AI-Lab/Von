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
});
