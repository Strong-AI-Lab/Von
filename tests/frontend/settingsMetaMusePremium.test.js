/** @jest-environment jsdom */

const fs = require('fs');
const path = require('path');

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#meta-settings-user' })),
    getWindowSessionId: jest.fn(() => 'meta-settings-window'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPagePath = '../../src/frontend/web/von_interface/static/js/settingsPage.js';
const settingsTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/settings_tab.html'),
    'utf8',
);

function jsonResponse(body, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => body,
    };
}

describe('Settings Meta Muse premium-model controls', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            premiumProvider: 'meta',
            openaiModel: null,
            metaModel: 'muse-spark-1.3',
            ollamaSelection: null,
        }));
        document.body.innerHTML = `
            <select id="premiumProviderSelect"><option value="openai">OpenAI</option><option value="meta" selected>Meta Muse</option></select>
            <input id="enableOpenAiPremiumToggle" type="checkbox" />
            <span id="enablePremiumModelLabel"></span>
            <div id="openaiProviderSettings"></div>
            <div id="metaProviderSettings"></div>
            <input id="metaApiKeyEnvVar" value="META_API_KEY" readonly />
            <button id="verifyMetaApiKeyButton"></button>
            <div id="metaStatusMessage"></div>
            <div id="metaModelsContainer"></div>
            <select id="metaModelSelect"><option value="muse-spark-1.3" selected>muse-spark-1.3</option></select>
            <button id="testMetaModelButton"></button>
            <button id="addMetaToWorkflowPoolButton"></button>
            <div id="metaModelEligibilityStatus"></div>
            <div id="metaModelStatusMessage"></div>
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

    test('offers Meta Muse only as an opt-in premium provider with the canonical key source', () => {
        const parsed = new DOMParser().parseFromString(settingsTemplate, 'text/html');
        expect(parsed.querySelector('#premiumProviderSelect option[value="meta"]')?.textContent).toBe('Meta Muse');
        expect(parsed.querySelector('#metaApiKeyEnvVar')?.value).toBe('META_API_KEY');
        expect(parsed.querySelector('#metaApiKeyEnvVar')?.readOnly).toBe(true);
        expect(parsed.querySelector('#serverDefaultLlmProvider option[value="meta"]')).toBeNull();
        expect(parsed.querySelector('#ragEmbedderProvider option[value="meta"]')).toBeNull();
    });

    test('uses Responses capabilities and probes the exact allowed Meta Muse provider/model', async () => {
        const { postJson } = await import(apiServicePath);
        postJson.mockResolvedValue({
            success: true,
            usable: true,
            model: 'muse-spark-1.3',
            model_parameters: { reasoning_effort: 'medium' },
            api_surface: 'responses',
            reason: 'Probe succeeded.',
        });
        global.fetch.mockResolvedValue(jsonResponse({
            success: true,
            parameters: {
                reasoning_effort: {
                    supported: true,
                    allowed_values: ['medium'],
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
                { provider: 'meta', model: 'muse-spark-1.3', model_parameters: { reasoning_effort: 'medium' } },
            ],
            effectiveEnabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest' },
                { provider: 'meta', model: 'muse-spark-1.3', model_parameters: { reasoning_effort: 'medium' } },
            ],
        });

        await __testOnly_refreshPremiumReasoningEffortControls('meta');
        const capabilityUrl = String(global.fetch.mock.calls[0][0]);
        expect(capabilityUrl).toContain('provider=meta');
        expect(capabilityUrl).toContain('model=muse-spark-1.3');
        expect(capabilityUrl).toContain('api_surface=responses');

        const effort = document.getElementById('openaiReasoningEffortSelect');
        effort.value = 'medium';
        __testOnly_renderModelScopeOverview();
        expect(document.getElementById('testMetaModelButton').disabled).toBe(false);
        expect(document.getElementById('metaModelEligibilityStatus').textContent).toContain('Allowed:');

        const result = await __testOnly_testSelectedPremiumModel('meta');
        expect(result).toMatchObject({
            usable: true,
            model: 'muse-spark-1.3',
            api_surface: 'responses',
        });
        expect(postJson).toHaveBeenCalledWith('/api/settings/meta/test_model', {
            api_key_env_var: 'META_API_KEY',
            model: 'muse-spark-1.3',
            model_parameters: { reasoning_effort: 'medium' },
        });
    });
});
