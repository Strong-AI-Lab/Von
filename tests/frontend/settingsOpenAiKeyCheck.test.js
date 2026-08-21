/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const settingsPagePath = '../../src/frontend/web/von_interface/static/js/settingsPage.js';

describe('settingsPage OpenAI key check', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        global.fetch = jest.fn();
        document.body.innerHTML = `
            <input id="openaiApiKeyEnvVar" value="OPENAI_API_KEY" />
            <button id="verifyOpenAiApiKeyButton" class="hidden" style="display:none">Verify</button>
            <div id="openaiStatusMessage"></div>
            <div id="openaiModelsContainer" class="hidden" style="display:none"></div>
            <input id="enableOpenAiPremiumToggle" type="checkbox" />
            <select id="openaiModelSelect"></select>
            <div id="openaiModelStatusMessage"></div>
        `;
    });

    test('reveals the verify button when the configured key exists', async () => {
        const { postJson } = await import(apiServicePath);
        const { checkOpenAiEnvVar } = await import(settingsPagePath);
        postJson.mockResolvedValue({
            exists: true,
            source: 'process'
        });

        const result = await checkOpenAiEnvVar();
        const verifyButton = document.getElementById('verifyOpenAiApiKeyButton');

        expect(result).toBe(true);
        expect(verifyButton.classList.contains('hidden')).toBe(false);
        expect(verifyButton.style.display).toBe('inline-block');
        expect(verifyButton.disabled).toBe(false);
        expect(document.getElementById('openaiStatusMessage').textContent).toBe(
            'OpenAI key found. Verify and test the selected model before enabling premium use.'
        );
    });

    test('keeps the verify button hidden when the configured key is absent', async () => {
        const { postJson } = await import(apiServicePath);
        const { checkOpenAiEnvVar } = await import(settingsPagePath);
        postJson.mockResolvedValue({
            exists: false
        });

        const result = await checkOpenAiEnvVar();
        const verifyButton = document.getElementById('verifyOpenAiApiKeyButton');

        expect(result).toBe(false);
        expect(verifyButton.classList.contains('hidden')).toBe(true);
        expect(verifyButton.style.display).toBe('none');
        expect(verifyButton.disabled).toBe(true);
    });

    test('loads models from a verify-button click without storing the click event as the model', async () => {
        const { postJson } = await import(apiServicePath);
        postJson.mockResolvedValue({
            success: true,
            models: ['gpt-5-mini']
        });

        await import(settingsPagePath);
        document.getElementById('verifyOpenAiApiKeyButton').click();
        await Promise.resolve();
        await Promise.resolve();

        const select = document.getElementById('openaiModelSelect');
        expect(postJson).toHaveBeenCalledWith('/api/settings/openai/verify', {
            api_key_env_var: 'OPENAI_API_KEY'
        });
        expect(Array.from(select.options).map((option) => option.value)).toEqual(['', 'gpt-5-mini']);
        expect(select.value).toBe('');
        expect(localStorage.getItem('von:openaiSelectedModel')).toBeNull();
        expect(localStorage.getItem('von:localModelPreference')).toBeNull();
    });
});
