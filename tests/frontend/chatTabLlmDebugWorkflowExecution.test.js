/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
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

    test('surfaces execution_id and links to trace endpoint', () => {
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

        showLlmDebugPopup(turnId);

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
});
