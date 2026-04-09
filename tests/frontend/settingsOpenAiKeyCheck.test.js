/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    postJson: jest.fn(),
    getJsonDetailed: jest.fn()
}));

import { checkOpenAiEnvVar } from '../../src/frontend/web/von_interface/static/js/settingsPage.js';

describe('settingsPage OpenAI key check', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <input id="openaiApiKeyEnvVar" value="OPENAI_API_KEY" />
            <button id="verifyOpenAiApiKeyButton" class="hidden" style="display:none">Verify</button>
            <div id="openaiStatusMessage"></div>
        `;
    });

    test('reveals the verify button when the configured key exists', async () => {
        const { postJson } = await import(
            '../../src/frontend/web/von_interface/static/js/apiService.js'
        );
        postJson.mockResolvedValue({
            exists: true,
            masked_value: 'sk-***',
            source: 'env'
        });

        const result = await checkOpenAiEnvVar();
        const verifyButton = document.getElementById('verifyOpenAiApiKeyButton');

        expect(result).toBe(true);
        expect(verifyButton.classList.contains('hidden')).toBe(false);
        expect(verifyButton.style.display).toBe('inline-block');
        expect(verifyButton.disabled).toBe(false);
    });

    test('keeps the verify button hidden when the configured key is absent', async () => {
        const { postJson } = await import(
            '../../src/frontend/web/von_interface/static/js/apiService.js'
        );
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
});
