/** @jest-environment jsdom */

describe('local model preferences', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
    });

    test('prefers the selected premium model when premium use is enabled locally', async () => {
        const {
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');
        setLocalPremiumModelUseEnabled(true);

        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'openai',
            model: 'gpt-5.4-mini',
            requestModel: 'openai:gpt-5.4-mini',
        });
    });

    test('prefers the locally selected Ollama model when premium use is disabled locally', async () => {
        const {
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOllamaSelection,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOllamaSelection({
            value: 'http://localhost:11434:llama3.1:8b',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
        });
        setLocalPremiumModelUseEnabled(false);

        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'ollama',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
            requestModel: 'ollama:llama3.1:8b',
        });
    });

    test('treats a stored Ollama selection as an active local override even when the legacy premium flag is absent', async () => {
        const {
            resolveLocalRequestedLlm,
            setStoredOllamaSelection,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOllamaSelection({
            value: 'http://localhost:11434:llama3.1:8b',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
        });

        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'ollama',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
            requestModel: 'ollama:llama3.1:8b',
        });
    });

    test('does not activate a stored premium model until premium use is enabled locally', async () => {
        const {
            resolveLocalRequestedLlm,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');

        expect(resolveLocalRequestedLlm()).toBeNull();
    });
});
