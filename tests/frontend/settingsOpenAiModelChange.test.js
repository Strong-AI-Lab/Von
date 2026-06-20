/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn(),
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
            <div id="openaiModelStatusMessage"></div>
            <select id="globalModelSelect"></select>
            <select id="currentUserSelect"></select>
            <select id="currentOrganisationSelect"></select>
            <select id="preferredLanguageSelect"></select>
            <div id="settingsStatusMessage"></div>
        `;
    });

    test('changing the selected OpenAI model persists, tests, and saves the real model id', async () => {
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
                    enabled_llms: [],
                    active_llm: null,
                    server_default_llm: null
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

        await import(settingsPagePath);
        document.dispatchEvent(new Event('DOMContentLoaded'));
        await waitUntil(() => changeListenerCount > 0);

        select.innerHTML = '<option value="">Select an OpenAI Model</option><option value="gpt-5-mini">gpt-5-mini</option>';
        select.value = 'gpt-5-mini';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        await waitUntil(() => testPayloads.length === 1 && savePayloads.length === 1);

        expect(localStorage.getItem('von:openaiSelectedModel')).toBe('gpt-5-mini');
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toMatchObject({
            openaiModel: 'gpt-5-mini'
        });
        expect(testPayloads[0]).toEqual({
            api_key_env_var: 'OPENAI_API_KEY',
            model: 'gpt-5-mini'
        });
        expect(savePayloads[0]).toMatchObject({
            openai_api_key_env_var: 'OPENAI_API_KEY'
        });
    });
});
