/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#settings-test-user' })),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPagePath = '../../src/frontend/web/von_interface/static/js/settingsPage.js';

function jsonResponse(body, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        statusText: status >= 200 && status < 300 ? 'OK' : 'Error',
        json: async () => body
    };
}

async function waitUntil(predicate, attempts = 50) {
    for (let index = 0; index < attempts; index += 1) {
        if (predicate()) return;
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
    throw new Error('Timed out waiting for condition');
}

describe('settingsPage OpenAI model change handling', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        global.fetch = jest.fn();
        document.body.innerHTML = `
            <div id="settingsConcernSummary"></div>
            <nav id="settingsSectionRail"></nav>
            <section id="premium-model-settings" data-settings-concern="models"></section>
            <div id="orgSelectorContainer"></div>
            <input id="openaiApiKeyEnvVar" value="OPENAI_API_KEY" />
            <button id="verifyOpenAiApiKeyButton" class="hidden" style="display:none">Verify</button>
            <div id="openaiStatusMessage"></div>
            <div id="openaiModelsContainer" class="hidden" style="display:none"></div>
            <input id="enableOpenAiPremiumToggle" type="checkbox" />
            <select id="openaiModelSelect"></select>
            <div id="openaiReasoningEffortContainer" class="hidden">
                <select id="openaiReasoningEffortSelect"></select>
            </div>
            <button id="testOpenAiModelButton" type="button">Test this model</button>
            <div id="openaiModelEligibilityStatus"></div>
            <div id="openaiModelCostSummary"></div>
            <div id="openaiModelStatusMessage"></div>
            <div id="browserChatModelSummary"></div>
            <div id="scopedPrimaryModelSummary"></div>
            <div id="sharedServerDefaultModelSummary"></div>
            <div id="workflowModelPoolList"></div>
            <div id="modelPoolStatusMessage"></div>
            <button id="addOpenAiToWorkflowPoolButton" type="button"></button>
            <button id="addOllamaToWorkflowPoolButton" type="button"></button>
            <select id="globalModelSelect"></select>
            <select id="currentUserSelect"></select>
            <select id="currentOrganisationSelect"></select>
            <select id="preferredLanguageSelect"></select>
            <div id="settingsStatusMessage"></div>
        `;
    });

    test('editing GPT reasoning while Gemma is active neither probes, saves, enrols, nor switches provider', async () => {
        const { getJsonDetailed, postJson } = await import(apiServicePath);
        const savePayloads = [];
        const testPayloads = [];
        let changeListenerCount = 0;
        const select = document.getElementById('openaiModelSelect');
        const originalAddEventListener = select.addEventListener.bind(select);

        select.addEventListener = (type, listener, options) => {
            if (type === 'change') changeListenerCount += 1;
            return originalAddEventListener(type, listener, options);
        };

        getJsonDetailed.mockImplementation(async (url) => {
            if (String(url).includes('/api/settings/ollama/models')) {
                return { data: { models: [] } };
            }
            if (String(url).includes('/api/settings/ollama/hosts')) {
                return { data: { hosts: [], active_host: null } };
            }
            if (String(url).includes('/api/settings/people')) {
                return { data: [] };
            }
            if (String(url).includes('/api/settings/organisations')) {
                return { data: [] };
            }
            return { data: {} };
        });
        postJson.mockImplementation(async (url, payload) => {
            if (url === '/api/settings/openai/test_model') {
                testPayloads.push(payload);
                return { usable: true, model: payload.model, reason: 'unit stub' };
            }
            if (url === '/api/settings/env_var/check') {
                return { exists: false };
            }
            if (url === '/von/api/session/set_organisation') {
                return { status: 'updated', organisation_id: null, namespace: null, role: 'user' };
            }
            return { success: true };
        });
        global.fetch.mockImplementation(async (url, options = {}) => {
            const target = String(url);
            const method = options.method || 'GET';
            if (target.startsWith('/api/settings/model_parameters/capabilities')) {
                return jsonResponse({
                    success: true,
                    parameters: {
                        reasoning_effort: {
                            supported: true,
                            allowed_values: ['low', 'medium', 'high']
                        }
                    }
                });
            }
            if (target.startsWith('/api/settings/') && method === 'POST') {
                savePayloads.push(JSON.parse(options.body || '{}'));
                return jsonResponse({
                    success: true,
                    message: 'Settings saved successfully!',
                    resolved_llm: { provider: 'openai', model: 'gpt-5-mini' }
                });
            }
            if (target.startsWith('/api/settings/')) {
                return jsonResponse({
                    openai_api_key_env_var: 'OPENAI_API_KEY',
                    enabled_llms: [
                        {
                            provider: 'ollama',
                            model: 'gemma4:latest',
                            host: 'http://127.0.0.1:11434'
                        },
                        {
                            provider: 'openai',
                            model: 'gpt-5.6-luna',
                            model_parameters: { reasoning_effort: 'low' }
                        }
                    ],
                    active_llm: {
                        provider: 'ollama',
                        model: 'gemma4:latest',
                        host: 'http://127.0.0.1:11434'
                    },
                    resolved_llm: {
                        provider: 'ollama',
                        model: 'gemma4:latest',
                        host: 'http://127.0.0.1:11434',
                        scope: 'user'
                    },
                    server_default_llm: { provider: 'ollama', model: 'qwen3:8b' }
                });
            }
            if (target.startsWith('/von/api/session/context')) {
                return jsonResponse({ role: 'user', namespace: null });
            }
            if (target.startsWith('/von/api/auth/status')) {
                return jsonResponse({ authenticated: false, browser_test_mode: { enabled: false } });
            }
            if (target.startsWith('/api/workflows/capability-index/status')) {
                return jsonResponse({ status: 'idle' });
            }
            return jsonResponse({});
        });

        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.6-luna',
            openaiModelParameters: { reasoning_effort: 'low' },
            ollamaSelection: {
                value: 'http://127.0.0.1:11434:gemma4:latest',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434'
            }
        }));
        localStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#settings-test-user',
            name: 'Settings Test User'
        }));

        await import(settingsPagePath);
        document.dispatchEvent(new Event('DOMContentLoaded'));
        await waitUntil(() => (
            changeListenerCount > 0
            && Array.from(document.getElementById('openaiReasoningEffortSelect').options)
                .some((option) => option.value === 'high')
        ));

        select.innerHTML = '<option value="">Select an OpenAI Model</option><option value="gpt-5.6-luna">gpt-5.6-luna</option>';
        select.value = 'gpt-5.6-luna';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        await waitUntil(() => localStorage.getItem('von:openaiSelectedModel') === 'gpt-5.6-luna');

        const reasoningEffort = document.getElementById('openaiReasoningEffortSelect');
        reasoningEffort.value = 'high';
        reasoningEffort.dispatchEvent(new Event('change', { bubbles: true }));
        await waitUntil(() => (
            JSON.parse(localStorage.getItem('von:localModelPreference'))
                ?.openaiModelParameters?.reasoning_effort === 'high'
        ));

        expect(localStorage.getItem('von:openaiSelectedModel')).toBe('gpt-5.6-luna');
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toMatchObject({
            activeSource: 'ollama',
            openaiModel: 'gpt-5.6-luna',
            openaiModelParameters: { reasoning_effort: 'high' },
            ollamaSelection: {
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434'
            }
        });
        expect(document.getElementById('enableOpenAiPremiumToggle').checked).toBe(false);
        expect(document.getElementById('browserChatModelSummary').textContent).toContain('gemma4:latest');
        expect(document.getElementById('workflowModelPoolList').textContent).toContain('low effort');
        expect(document.getElementById('addOpenAiToWorkflowPoolButton').textContent).toBe(
            'Update allowed model entry'
        );
        expect(document.getElementById('openaiModelEligibilityStatus').textContent).toContain('Allowed:');
        expect(document.getElementById('testOpenAiModelButton').disabled).toBe(false);
        expect(testPayloads).toEqual([]);
        expect(savePayloads).toEqual([]);

        document.getElementById('testOpenAiModelButton').click();
        await waitUntil(() => testPayloads.length === 1);
        expect(testPayloads[0]).toEqual({
            api_key_env_var: 'OPENAI_API_KEY',
            model: 'gpt-5.6-luna',
            model_parameters: { reasoning_effort: 'high' }
        });
        expect(savePayloads).toEqual([]);
    });
});
