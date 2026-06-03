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

describe('chat thinking diagnostics copy control', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <button id="copyThinkingDiagnosticsButton"
                data-thinking-role="copy"
                aria-label="Copy diagnostic reference">Copy reference</button>
        `;
        Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            writable: true,
            value: { writeText: jest.fn().mockResolvedValue(undefined) }
        });
    });

    test('copies a compact live progress locator instead of diagnostic histories', async () => {
        const chatTab = require(chatTabModulePath);
        const button = document.getElementById('copyThinkingDiagnosticsButton');

        chatTab.__testOnly_setActiveChatSession('session-thinking-copy', 'Diagnostics Session');

        const noisyRequest = {
            clientRequestId: 'req-thinking-copy-1',
            promptRaw: 'do not copy this prompt body',
            latestProgress: {
                request_id: 'req-thinking-copy-1',
                status: 'running',
                stage: 'workflow_execution',
                phase: 'tool_use',
                elapsed_ms: 42.8
            },
            activityHistory: [{ event: 'large activity payload' }],
            progressEvents: [{ event: 'large progress payload' }],
            phaseHistory: [{ event: 'large phase payload' }],
            toolHistory: [{ tool: 'large tool payload' }],
            workflowDiscovery: { candidates: [{ workflow_id: 'wf-large' }] },
            stageDiagnostics: [{ prompt: 'large stage prompt' }]
        };

        const copied = await chatTab.__testOnly_copyActiveThinkingDiagnostics(button, noisyRequest);

        expect(copied).toBe(true);
        expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);

        const payload = JSON.parse(navigator.clipboard.writeText.mock.calls[0][0]);
        expect(payload.schema_version).toBe('turn_live_progress_locator.v1');
        expect(payload.request_id).toBe('req-thinking-copy-1');
        expect(payload.chat_session_id).toBe('session-thinking-copy');
        expect(payload.latest_progress_summary).toEqual(expect.objectContaining({
            status: 'running',
            stage: 'workflow_execution',
            phase: 'tool_use',
            elapsed_ms: 43
        }));
        expect(payload.mcp_access).toEqual(expect.objectContaining({
            turn_execution_get_live_progress: expect.any(Object),
            turn_execution_get_diagnostics: expect.any(Object)
        }));

        expect(payload.prompt_preview).toBeUndefined();
        expect(payload.activity_history).toBeUndefined();
        expect(payload.progress_events).toBeUndefined();
        expect(payload.phase_history).toBeUndefined();
        expect(payload.tool_history).toBeUndefined();
        expect(payload.workflow_discovery).toBeUndefined();
        expect(payload.stage_diagnostics).toBeUndefined();
    });

    test('does not fall back to copying a snapshot when no request reference exists', async () => {
        const chatTab = require(chatTabModulePath);
        const { showToast } = require('../../src/frontend/web/von_interface/static/js/utils/toast.js');
        const button = document.getElementById('copyThinkingDiagnosticsButton');

        const copied = await chatTab.__testOnly_copyActiveThinkingDiagnostics(button, {
            promptRaw: 'still should not be copied',
            progressEvents: [{ event: 'large progress payload' }]
        });

        expect(copied).toBe(false);
        expect(navigator.clipboard.writeText).not.toHaveBeenCalled();
        expect(showToast).toHaveBeenCalledWith(
            'No diagnostic reference is available yet.',
            'info'
        );
    });
});
