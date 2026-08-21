/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#gemini-settings-user' })),
    getWindowSessionId: jest.fn(() => 'gemini-settings-window'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPagePath = '../../src/frontend/web/von_interface/static/js/settingsPage.js';

function jsonResponse(body, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
    };
}

describe('Settings Gemini premium-model controls', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            premiumProvider: 'gemini',
            openaiModel: null,
            geminiModel: 'gemini-3.7-flash',
            ollamaSelection: null,
        }));
        document.body.innerHTML = `
            <select id="premiumProviderSelect"><option value="openai">OpenAI</option><option value="gemini" selected>Gemini</option></select>
            <input id="enableOpenAiPremiumToggle" type="checkbox" />
            <span id="enablePremiumModelLabel"></span>
            <div id="openaiProviderSettings"></div>
            <div id="geminiProviderSettings"></div>
            <input id="geminiApiKeyEnvVar" value="GEMINI_API_KEY" />
            <button id="verifyGeminiApiKeyButton"></button>
            <div id="geminiStatusMessage"></div>
            <div id="geminiModelsContainer"></div>
            <select id="geminiModelSelect"><option value="gemini-3.7-flash" selected>gemini-3.7-flash</option></select>
            <button id="testGeminiModelButton"></button>
            <button id="addGeminiToWorkflowPoolButton"></button>
            <div id="geminiModelEligibilityStatus"></div>
            <div id="geminiModelStatusMessage"></div>
            <div id="openaiReasoningEffortContainer" class="hidden"><select id="openaiReasoningEffortSelect"></select></div>
            <div id="openaiModelCostSummary"></div>
            <div id="browserChatModelSummary"></div>
            <div id="effectiveScopedModelSummary"></div>
            <div id="scopedPrimaryModelSummary"></div>
            <div id="sharedServerDefaultModelSummary"></div>
            <div id="workflowModelPoolList"></div>
            <div id="modelPoolStatusMessage"></div>
            <button id="saveModelPoolButton"></button>
            <button id="useBrowserModelAsScopedPrimaryButton"></button>
            <button id="restoreScopedPrimaryButton"></button>
        `;
        global.fetch = jest.fn();
    });

    test('uses Gemini Interactions capabilities and probes the exact allowed provider/model', async () => {
        const { postJson } = await import(apiServicePath);
        postJson.mockResolvedValue({
            success: true,
            usable: true,
            model: 'gemini-3.7-flash',
            model_parameters: { reasoning_effort: 'high' },
            api_surface: 'interactions',
            reason: 'Probe succeeded.',
        });
        global.fetch.mockResolvedValue(jsonResponse({
            success: true,
            parameters: {
                reasoning_effort: {
                    supported: true,
                    allowed_values: ['low', 'medium', 'high'],
                },
            },
        }));

        const {
            __testOnly_refreshPremiumReasoningEffortControls,
            __testOnly_renderModelScopeOverview,
            __testOnly_setModelScopeState,
            __testOnly_testSelectedPremiumModel,
        } = await import(settingsPagePath);
        __testOnly_setModelScopeState({
            resolvedLlm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
            enabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest' },
                { provider: 'gemini', model: 'gemini-3.7-flash', model_parameters: { reasoning_effort: 'high' } },
            ],
            effectiveEnabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest' },
                { provider: 'gemini', model: 'gemini-3.7-flash', model_parameters: { reasoning_effort: 'high' } },
            ],
        });

        await __testOnly_refreshPremiumReasoningEffortControls('gemini');
        const capabilityUrl = String(global.fetch.mock.calls[0][0]);
        expect(capabilityUrl).toContain('provider=gemini');
        expect(capabilityUrl).toContain('model=gemini-3.7-flash');
        expect(capabilityUrl).toContain('api_surface=interactions');

        const effort = document.getElementById('openaiReasoningEffortSelect');
        effort.value = 'high';
        __testOnly_renderModelScopeOverview();
        expect(document.getElementById('testGeminiModelButton').disabled).toBe(false);
        expect(document.getElementById('geminiModelEligibilityStatus').textContent).toContain('Allowed:');

        const result = await __testOnly_testSelectedPremiumModel('gemini');
        expect(result).toMatchObject({
            usable: true,
            model: 'gemini-3.7-flash',
            api_surface: 'interactions',
        });
        expect(postJson).toHaveBeenCalledWith('/api/settings/gemini/test_model', {
            api_key_env_var: 'GEMINI_API_KEY',
            model: 'gemini-3.7-flash',
            model_parameters: { reasoning_effort: 'high' },
        });
    });
});
