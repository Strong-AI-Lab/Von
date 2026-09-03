/** @jest-environment jsdom */

describe('local model preferences', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
    });

    test('keeps an OpenRouter slug and provider distinct from OpenAI and Ollama', async () => {
        const {
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOpenRouterSelectedModel,
            setStoredPremiumModelProvider,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenRouterSelectedModel('anthropic/claude-test');
        setStoredPremiumModelProvider('openrouter');
        setLocalPremiumModelUseEnabled(true, 'openrouter');

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openrouter',
            premiumProvider: 'openrouter',
            openaiModel: null,
            openrouterModel: 'anthropic/claude-test',
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'openrouter',
            model: 'anthropic/claude-test',
            requestModel: 'openrouter:anthropic/claude-test',
        });
    });

    test('prefers the selected premium model when premium use is enabled locally', async () => {
        const {
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');
        setLocalPremiumModelUseEnabled(true);

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'openai',
            model: 'gpt-5.4-mini',
            requestModel: 'openai:gpt-5.4-mini',
        });
    });

    test('stores OpenAI reasoning effort with the selected premium model', async () => {
        const {
            getStoredLocalModelPreference,
            normaliseModelParameters,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOpenAiModelParameters,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.5');
        setStoredOpenAiModelParameters({ reasoning: { effort: 'low' } });
        setLocalPremiumModelUseEnabled(true);

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.5',
            openaiModelParameters: { reasoning_effort: 'low' },
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'openai',
            model: 'gpt-5.5',
            requestModel: 'openai:gpt-5.5',
            model_parameters: { reasoning_effort: 'low' },
        });
        expect(normaliseModelParameters(
            { reasoning_effort: 'future' },
            { parameters: { reasoning_effort: { allowed_values: ['future'] } } },
        )).toEqual({ reasoning_effort: 'future' });
        expect(normaliseModelParameters(
            { reasoning_effort: 'xhigh' },
            { parameters: { reasoning_effort: { allowed_values: ['future'] } } },
        )).toBeNull();
    });

    test('prefers the locally selected Ollama model when premium use is disabled locally', async () => {
        const {
            getStoredLocalModelPreference,
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

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: null,
            ollamaSelection: {
                value: 'http://localhost:11434:llama3.1:8b',
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'ollama',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
            requestModel: 'ollama:llama3.1:8b',
        });
    });

    test('treats a stored Ollama selection as an active local override even when the legacy premium flag is absent', async () => {
        const {
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setStoredOllamaSelection,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOllamaSelection({
            value: 'http://localhost:11434:llama3.1:8b',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
        });

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: null,
            ollamaSelection: {
                value: 'http://localhost:11434:llama3.1:8b',
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
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
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: null,
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toBeNull();
    });

    test('does not persist browser events as selected OpenAI models', async () => {
        const {
            getStoredLocalModelPreference,
            getStoredOpenAiSelectedModel,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');
        setLocalPremiumModelUseEnabled(true);
        setStoredOpenAiSelectedModel(new Event('click'));

        expect(getStoredOpenAiSelectedModel()).toBe('');
        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: null,
            ollamaSelection: null,
        });
        expect(localStorage.getItem('von:openaiSelectedModel')).toBeNull();
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: null,
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toBeNull();
    });

    test('cleans object-string OpenAI model artefacts from stored browser preferences', async () => {
        const {
            getStoredLocalModelPreference,
            getStoredOpenAiSelectedModel,
            resolveLocalRequestedLlm,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: '[object PointerEvent]',
            ollamaSelection: null,
        }));
        localStorage.setItem('von:openaiSelectedModel', '[object PointerEvent]');

        expect(getStoredOpenAiSelectedModel()).toBe('');
        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: null,
            ollamaSelection: null,
        });
        expect(localStorage.getItem('von:openaiSelectedModel')).toBeNull();
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: null,
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toBeNull();
    });

    test('marks disabled premium with no Ollama selection as an unavailable local model state', async () => {
        const {
            applyLocalModelPreferenceOverlay,
            clearStoredOllamaSelection,
            getEffectiveLocalModelPreference,
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredOllamaSelection,
            setStoredOpenAiSelectedModel,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredOpenAiSelectedModel('gpt-5.4-mini');
        setStoredOllamaSelection({
            value: 'http://localhost:11434:llama3.1:8b',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
        });
        setLocalPremiumModelUseEnabled(false);
        clearStoredOllamaSelection();

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        });
        expect(getEffectiveLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
            requestedLlm: null,
            modelUnavailable: true,
            modelUnavailableReason: 'premium_disabled_no_ollama_model',
        });
        expect(resolveLocalRequestedLlm()).toBeNull();
        expect(applyLocalModelPreferenceOverlay({
            active_llm: { provider: 'openai', model: 'gpt-5.4-mini' },
        })).toEqual({
            active_llm: null,
            local_model_unavailable_reason: 'premium_disabled_no_ollama_model',
        });
    });

    test('migrates legacy premium-openai state into the canonical local model preference', async () => {
        const {
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        localStorage.setItem('von:openaiSelectedModel', 'gpt-5.4-mini');
        localStorage.setItem('von:premiumModelUseEnabled', 'true');

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        });
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'openai',
            model: 'gpt-5.4-mini',
            requestModel: 'openai:gpt-5.4-mini',
        });
    });

    test('migrates legacy ollama state without the premium flag into canonical ollama-active state', async () => {
        const {
            getEffectiveLocalModelPreference,
            resolveLocalRequestedLlm,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        localStorage.setItem('von:ollamaSelection', JSON.stringify({
            value: 'http://localhost:11434:llama3.1:8b',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
        }));

        expect(getEffectiveLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: null,
            ollamaSelection: {
                value: 'http://localhost:11434:llama3.1:8b',
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
            requestedLlm: {
                provider: 'ollama',
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
                requestModel: 'ollama:llama3.1:8b',
            },
            modelUnavailable: false,
            modelUnavailableReason: null,
        });
        expect(resolveLocalRequestedLlm()).toEqual({
            provider: 'ollama',
            model: 'llama3.1:8b',
            host: 'http://localhost:11434',
            requestModel: 'ollama:llama3.1:8b',
        });
    });

    test('footer overlay and request-model resolution derive from the same canonical local preference', async () => {
        const {
            applyLocalModelPreferenceOverlay,
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

        expect(applyLocalModelPreferenceOverlay({ resolved_llm: null })).toEqual({
            resolved_llm: null,
            active_llm: {
                provider: 'ollama',
                model: 'llama3.1:8b',
                host: 'http://localhost:11434',
            },
        });
    });

    test('persists Gemini as a first-class premium provider and resolves its exact request identity', async () => {
        const {
            buildLocalModelRequestFields,
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredGeminiModelParameters,
            setStoredGeminiSelectedModel,
            setStoredPremiumModelProvider,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredPremiumModelProvider('gemini');
        setStoredGeminiSelectedModel('gemini-3.7-flash');
        setStoredGeminiModelParameters({ reasoning_effort: 'high' });
        setLocalPremiumModelUseEnabled(true, 'gemini');

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'gemini',
            premiumProvider: 'gemini',
            openaiModel: null,
            geminiModel: 'gemini-3.7-flash',
            geminiModelParameters: { reasoning_effort: 'high' },
            ollamaSelection: null,
        });
        const requested = resolveLocalRequestedLlm();
        expect(requested).toEqual({
            provider: 'gemini',
            model: 'gemini-3.7-flash',
            requestModel: 'gemini:gemini-3.7-flash',
            model_parameters: { reasoning_effort: 'high' },
        });
        expect(buildLocalModelRequestFields(requested)).toEqual({
            model: 'gemini-3.7-flash',
            model_provider: 'gemini',
            model_parameters: { reasoning_effort: 'high' },
        });
    });

    test('persists Meta Muse as a first-class premium provider and resolves its exact request identity', async () => {
        const {
            buildLocalModelRequestFields,
            getStoredLocalModelPreference,
            resolveLocalRequestedLlm,
            setLocalPremiumModelUseEnabled,
            setStoredMetaModelParameters,
            setStoredMetaSelectedModel,
            setStoredPremiumModelProvider,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredPremiumModelProvider('meta');
        setStoredMetaSelectedModel('muse-spark-1.3');
        setStoredMetaModelParameters({ reasoning_effort: 'medium' });
        setLocalPremiumModelUseEnabled(true, 'meta');

        expect(getStoredLocalModelPreference()).toEqual({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'meta',
            premiumProvider: 'meta',
            openaiModel: null,
            metaModel: 'muse-spark-1.3',
            metaModelParameters: { reasoning_effort: 'medium' },
            ollamaSelection: null,
        });
        const requested = resolveLocalRequestedLlm();
        expect(requested).toEqual({
            provider: 'meta',
            model: 'muse-spark-1.3',
            requestModel: 'meta:muse-spark-1.3',
            model_parameters: { reasoning_effort: 'medium' },
        });
        expect(buildLocalModelRequestFields(requested)).toEqual({
            model: 'muse-spark-1.3',
            model_provider: 'meta',
            model_parameters: { reasoning_effort: 'medium' },
        });
    });

    test('rejects object events as premium providers without replacing the stored provider', async () => {
        const {
            getStoredPremiumModelProvider,
            setStoredPremiumModelProvider,
        } = await import('../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js');

        setStoredPremiumModelProvider('gemini');
        setStoredPremiumModelProvider(new Event('change'));

        expect(getStoredPremiumModelProvider()).toBe('gemini');
        expect(JSON.parse(localStorage.getItem('von:localModelPreference'))).toMatchObject({
            premiumProvider: 'gemini',
        });
    });

    test('builds explicit provider fields for existing OpenAI and Ollama requests', async () => {
        const { buildLocalModelRequestFields } = await import(
            '../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js'
        );

        expect(buildLocalModelRequestFields({
            provider: 'openai',
            model: 'gpt-5.6-luna',
            requestModel: 'openai:gpt-5.6-luna',
        })).toEqual({
            model: 'gpt-5.6-luna',
            model_provider: 'openai',
        });
        expect(buildLocalModelRequestFields({
            provider: 'ollama',
            model: 'gemma4:latest',
            requestModel: 'ollama:gemma4:latest',
        })).toEqual({
            model: 'gemma4:latest',
            model_provider: 'ollama',
        });
    });

    test('classifies only configured remote providers as premium', async () => {
        const { isPremiumModelProvider } = await import(
            '../../src/frontend/web/von_interface/static/js/utils/localModelPreferences.js'
        );

        expect(['openai', 'openrouter', 'gemini', 'meta'].every(isPremiumModelProvider)).toBe(true);
        expect(isPremiumModelProvider(' OpenAI ')).toBe(true);
        expect(isPremiumModelProvider('ollama')).toBe(false);
        expect(isPremiumModelProvider('unknown-provider')).toBe(false);
    });
});
