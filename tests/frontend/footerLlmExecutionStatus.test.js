/** @jest-environment jsdom */

const fs = require('fs');
const path = require('path');

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';
const suppressTooltipsPath = '../../src/frontend/web/von_interface/static/js/suppressTooltips.js';
const stylesPath = path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css');

function installFetchMock(options = {}) {
    const {
        settingsPayload = { resolved_llm: { provider: 'openai', model: 'gpt-5.4-mini' } },
        llmInfoOk = true,
        llmInfoPayload = {
            provider: 'openai',
            model: 'gpt-5.4-mini',
            status: 'ready',
            details: {}
        },
        dbInfoPayload = {},
        authPayload = { authenticated: true, email: 'researcher@example.test' },
        capabilityStatusPayload = null,
    } = options;
    const effectiveCapabilityStatusPayload = capabilityStatusPayload || {
        ready: true,
        status: 'ready',
        checked_at_utc: '2026-07-14T04:00:00Z',
    };
    global.fetch = jest.fn(async (url) => {
        const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
        const parsed = new URL(rawUrl, 'http://localhost');
        const path = parsed.pathname;
        if (path === '/api/settings/' || path === '/api/settings') {
            return {
                ok: true,
                json: async () => settingsPayload
            };
        }
        if (path === '/api/settings/llm/info') {
            return {
                ok: llmInfoOk,
                json: async () => llmInfoPayload
            };
        }
        if (path === '/api/settings/db/info') {
            return { ok: true, json: async () => dbInfoPayload };
        }
        if (path === '/api/workflows/capability-index/status') {
            return { ok: true, json: async () => effectiveCapabilityStatusPayload };
        }
        if (path === '/von/api/auth/status' || path === '/api/auth/status') {
            return { ok: true, json: async () => authPayload };
        }
        return { ok: true, json: async () => ({}) };
    });
}

async function flushUiTicks(ticks = 4) {
    for (let i = 0; i < ticks; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
}

async function waitForModelSegment(predicate, maxTicks = 24) {
    for (let i = 0; i < maxTicks; i += 1) {
        const modelSegment = Array.from(document.querySelectorAll('.footer-segment'))
            .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'Model:');
        if (modelSegment && predicate(modelSegment)) {
            return modelSegment;
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return Array.from(document.querySelectorAll('.footer-segment'))
        .find((seg) => seg.querySelector('.footer-label-inline')?.textContent?.trim() === 'Model:');
}

async function waitForWorkflowIndexBadge(maxTicks = 40) {
    for (let i = 0; i < maxTicks; i += 1) {
        const badge = document.querySelector('.workflow-index-status-badge');
        if (badge) return badge;
        await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return document.querySelector('.workflow-index-status-badge');
}

function bindExecutionTelemetry(payload, options = {}) {
    const generation = options.generation || 1;
    const currentGeneration = options.currentGeneration || generation;
    const conversationSessionId = options.conversationSessionId || 'conversation-a';
    const currentConversationSessionId = options.currentConversationSessionId || conversationSessionId;
    const userConceptId = options.userConceptId ?? null;
    const currentUserConceptId = options.currentUserConceptId ?? userConceptId;
    const organisationConceptId = options.organisationConceptId ?? null;
    const currentOrganisationConceptId = options.currentOrganisationConceptId ?? organisationConceptId;
    const configuration = options.configuration || { provider: 'openai', model: 'gpt-5.4-mini' };
    const currentConfiguration = options.currentConfiguration || configuration;
    const windowSessionId = 'ws-footer-execution';
    sessionStorage.setItem('von_window_session_id', windowSessionId);
    window.__vonCurrentLlmExecutionContextBinding = {
        schema_version: 'llm_execution_context_binding.v1',
        generation: currentGeneration,
        window_session_id: windowSessionId,
        user_concept_id: currentUserConceptId,
        organisation_concept_id: currentOrganisationConceptId,
        conversation_session_id: currentConversationSessionId,
        configuration: currentConfiguration,
    };
    return {
        ...payload,
        context_binding: {
            schema_version: 'llm_execution_context_binding.v1',
            generation,
            window_session_id: windowSessionId,
            user_concept_id: userConceptId,
            organisation_concept_id: organisationConceptId,
            conversation_session_id: conversationSessionId,
            configuration,
        },
    };
}

describe('footer latest LLM execution status', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <button class="tab-button" data-tab="settingsTab" type="button">Settings</button>
            <iframe id="settingsFrame"></iframe>
            <div class="footer-container"><p id="modelInfoFooter"></p></div>
        `;
        localStorage.clear();
        sessionStorage.clear();
        window.__vonLatestLlmExecutionTelemetry = null;
        delete window.__vonCurrentLlmExecutionContextBinding;
        delete window.__vonWorkflowCapabilityIndexStatusCoordinator;
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
        installFetchMock();
        require(suppressTooltipsPath);
    });

    afterEach(() => {
        if (typeof window.__VON_RESTORE_TITLES === 'function') {
            window.__VON_RESTORE_TITLES();
        }
        jest.restoreAllMocks();
        localStorage.clear();
        sessionStorage.clear();
        window.__vonLatestLlmExecutionTelemetry = null;
        delete window.__vonCurrentLlmExecutionContextBinding;
        delete window.__vonWorkflowCapabilityIndexStatusCoordinator;
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
        delete window.__VON_RESTORE_TITLES;
    });

    test('shows unselected model when premium is disabled and no Ollama model is selected', async () => {
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.4-mini',
            ollamaSelection: null,
        }));
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.classList.contains('fatal')
            && seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'unselected'
        ));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('llm-status-badge')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(true);

        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton.getAttribute('aria-label')).toContain('Model unselected');
        expect(modelButton.getAttribute('aria-label')).toContain('Configured status Unselected');
        expect(modelButton.getAttribute('aria-label')).toContain('Open language model settings');
        expect(modelButton.title).toContain('Configured status: Unselected');
        expect(modelButton.title).toContain('No usable model configured: premium model use is disabled and no Ollama model is selected.');

        const llmInfoCalls = global.fetch.mock.calls.filter(([url]) => {
            const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
            return new URL(rawUrl, 'http://localhost').pathname === '/api/settings/llm/info';
        });
        expect(llmInfoCalls).toHaveLength(0);
    });

    test('ready model exposes its model and status and sends window-scoped actor headers', async () => {
        sessionStorage.setItem('von_window_session_id', 'ws_footer_actor_a');
        sessionStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#footer-user-a',
            name: 'Footer User A',
        }));
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.classList.contains('ready')
            && seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gpt-5.4-mini'
        ));
        const modelButton = modelSegment?.querySelector('.concept-footer-button');
        expect(modelButton?.getAttribute('aria-label')).toContain('Model gpt-5.4-mini');
        expect(modelButton?.getAttribute('aria-label')).toContain('Configured status Ready');
        expect(modelButton?.getAttribute('aria-label')).toContain('Open language model settings');

        const actorScopedCalls = global.fetch.mock.calls.filter(([url]) => {
            const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
            const pathname = new URL(rawUrl, 'http://localhost').pathname;
            return pathname === '/api/settings/' || pathname === '/api/settings/llm/info';
        });
        expect(actorScopedCalls.length).toBeGreaterThanOrEqual(2);
        for (const [, options] of actorScopedCalls) {
            expect(options?.headers).toMatchObject({
                'X-Von-Window-Session': 'ws_footer_actor_a',
                'X-User-Concept-ID': '#V#footer-user-a',
            });
        }
    });

    test('shows a recovered fallback as warning and exposes its reason across the whole badge', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            call_models: ['gpt-5.4-mini', 'granite3.3:2b'],
            fallback_used: true,
            primary_failure_kind: null,
            primary_failure_reason: null,
            recovered_failure_kind: 'quota_exhausted',
            recovered_failure_reason: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.',
            warnings: ['Backend error: OpenAI quota exhausted (insufficient_quota): exceeded your current quota.']
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.classList.contains('warning')
            && seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'granite3.3:2b'
        ));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('llm-status-badge')).toBe(true);
        expect(modelSegment.classList.contains('warning')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
        expect(modelSegment.getAttribute('data-keep-title')).toBe('true');
        expect(modelSegment.title).toContain('Last execution status: Fallback Recovered');
        expect(modelSegment.title).toContain('Recovered failure kind: quota_exhausted');
        expect(modelSegment.title).toContain('Recovered failure reason: OpenAI quota exhausted');

        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton.textContent.trim()).toBe('granite3.3:2b');
        expect(modelButton.getAttribute('data-keep-title')).toBe('true');
        expect(modelButton.getAttribute('aria-label')).toContain('Model granite3.3:2b');
        expect(modelButton.getAttribute('aria-label')).toContain('Last execution status Fallback Recovered');
        expect(modelButton.getAttribute('aria-label')).toContain('Recovered failure reason OpenAI quota exhausted');
        expect(modelButton.title).toContain('Configured model: gpt-5.4-mini');
        expect(modelButton.title).toContain('Executed provider: ollama');
        expect(modelButton.title).toContain('Executed model: granite3.3:2b');
        expect(modelButton.title).toContain('Recovered failure kind: quota_exhausted');
        expect(modelButton.title).toContain('Recovered failure reason: OpenAI quota exhausted (insufficient_quota): exceeded your current quota.');
    });

    test('does not turn footer red for OpenAI alias resolution (dated snapshot)', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini-2026-03-17',
            actual_provider: 'openai',
            call_models: ['gpt-5.4-mini-2026-03-17'],
            fallback_used: false,
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) =>
            seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gpt-5.4-mini'
        );
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('fatal')).toBe(false);
        expect(modelSegment.classList.contains('warning')).toBe(false);
        expect(modelSegment.classList.contains('ready')).toBe(true);
    });

    test('does not turn footer red for presenter output warnings alone', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini',
            actual_provider: 'openai',
            call_models: ['gpt-5.4-mini'],
            fallback_used: false,
            primary_failure_reason: null,
            warnings: [
                'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
            ]
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) =>
            seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gpt-5.4-mini'
        );
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('fatal')).toBe(false);
        expect(modelSegment.classList.contains('warning')).toBe(false);
        expect(modelSegment.classList.contains('ready')).toBe(true);
    });

    test('shows warning (not fatal) when fallback succeeds with a different model', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            call_models: ['gpt-5.4-mini', 'granite3.3:2b'],
            fallback_used: true,
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) =>
            seg.classList.contains('warning')
        );
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('llm-status-badge')).toBe(true);
        expect(modelSegment.classList.contains('warning')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
    });

    test('keeps an unrecovered terminal execution failure fatal', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini',
            actual_provider: 'openai',
            call_models: ['gpt-5.4-mini'],
            fallback_used: false,
            primary_failure_kind: 'request_timeout',
            primary_failure_reason: 'The model request timed out.'
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => seg.classList.contains('fatal'));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('fatal')).toBe(true);
        expect(modelSegment.classList.contains('warning')).toBe(false);
        expect(modelSegment.title).toContain('Last execution status: Execution Error');
        expect(modelSegment.title).toContain('Failure reason: The model request timed out.');
    });

    test('shows a represented enabled-pool model selection as warning rather than mismatch failure', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            call_models: ['gemma4:latest'],
            fallback_used: false,
            selection_mode: 'enabled_settings_candidate',
            follows_active_llm: false,
            primary_failure_reason: null
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => seg.classList.contains('warning'));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('warning')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
        expect(modelSegment.title).toContain('Last execution status: Model Selection Active');
    });

    test('does not let an unknown selection mode suppress a genuine model mismatch', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();
        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            call_models: ['gemma4:latest'],
            fallback_used: false,
            selection_mode: 'unexpected_future_mode',
            follows_active_llm: false,
            primary_failure_reason: null,
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry,
        }));

        const modelSegment = await waitForModelSegment((seg) => seg.classList.contains('fatal'));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('fatal')).toBe(true);
        expect(modelSegment.classList.contains('warning')).toBe(false);
    });

    test('does not treat an Ollama provider-qualified model reference as a mismatch', async () => {
        installFetchMock({
            settingsPayload: { resolved_llm: { provider: 'ollama', model: 'gemma4:latest' } },
            llmInfoPayload: {
                provider: 'ollama',
                model: 'gemma4:latest',
                status: 'ready',
                details: {}
            }
        });
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'ollama/gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'OLLAMA',
            call_models: ['gemma4:latest'],
            fallback_used: false
        }, { configuration: { provider: 'ollama', model: 'gemma4:latest' } });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gemma4:latest'
        ));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('ready')).toBe(true);
        expect(modelSegment.classList.contains('warning')).toBe(false);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
    });

    test('shows a probe disagreement warning when the same model succeeded in live telemetry', async () => {
        installFetchMock({
            settingsPayload: { resolved_llm: { provider: 'ollama', model: 'gemma4:latest' } },
            llmInfoPayload: {
                provider: 'ollama',
                model: 'gemma4:latest',
                status: 'error',
                error: 'The model health probe timed out.',
                details: { host: 'http://127.0.0.1:11434' }
            }
        });
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'ollama/gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            call_models: ['gemma4:latest'],
            call_providers: ['ollama'],
            execution_succeeded: true,
            fallback_used: false,
            primary_failure_kind: null,
            primary_failure_reason: null
        };
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.classList.contains('warning')
            && seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gemma4:latest'
        ));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('warning')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
        expect(modelSegment.classList.contains('error')).toBe(false);
        expect(modelSegment.getAttribute('data-keep-title')).toBe('true');
        expect(modelSegment.title).toContain('Configured status: Error');
        expect(modelSegment.title).toContain('Error: The model health probe timed out.');
        expect(modelSegment.title).toContain('Last execution status: Live Execution Succeeded');
        expect(modelSegment.title).toContain('The configured health probe failed, but the same model completed the latest live LLM call successfully.');

        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton.getAttribute('aria-label')).toContain('Last execution status Live Execution Succeeded');
        expect(modelButton.title).toBe(modelSegment.title);
    });

    test('ignores an unbound legacy mismatch from a previous model selection', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);
        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gemma4:latest',
            actual_model: 'gemma4:latest',
            actual_provider: 'ollama',
            call_models: ['gemma4:latest'],
            primary_failure_reason: 'Old Gemma execution failed.',
        };
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry,
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gpt-5.4-mini'
        ));
        expect(modelSegment.classList.contains('ready')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
    });

    test.each([
        ['actor', {
            userConceptId: '#V#old-user',
            currentUserConceptId: '#V#current-user',
        }],
        ['conversation', {
            conversationSessionId: 'conversation-old',
            currentConversationSessionId: 'conversation-current',
        }],
        ['context generation', {
            generation: 3,
            currentGeneration: 4,
        }],
        ['model configuration', {
            configuration: { provider: 'ollama', model: 'gemma4:latest' },
            currentConfiguration: { provider: 'openai', model: 'gpt-5.4-mini' },
        }],
    ])('ignores telemetry bound to a previous %s', async (_label, bindingOptions) => {
        if (bindingOptions.currentUserConceptId) {
            sessionStorage.setItem('von_current_user', JSON.stringify({
                concept_id: bindingOptions.currentUserConceptId,
                name: 'Current User',
            }));
        }
        const { setModelInfoFooterText } = require(domUtilsPath);
        await setModelInfoFooterText();

        const oldConfiguration = bindingOptions.configuration;
        const oldModel = oldConfiguration?.model || 'gpt-5.4-mini';
        const oldProvider = oldConfiguration?.provider || 'openai';
        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: oldModel,
            actual_model: oldModel,
            actual_provider: oldProvider,
            call_models: [oldModel],
            primary_failure_reason: `Old ${_label} execution failed.`,
        }, bindingOptions);
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry,
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'gpt-5.4-mini'
        ));
        expect(modelSegment.classList.contains('ready')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);
    });

    test('shows fatal when actual model genuinely differs from requested', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-4o-mini',
            actual_provider: 'openai',
            call_models: ['gpt-4o-mini'],
            fallback_used: false,
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) =>
            seg.classList.contains('fatal')
        );
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('fatal')).toBe(true);
    });

    test('shows warning rather than fatal for an explicit stage-model override', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = bindExecutionTelemetry({
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-4o-mini',
            actual_provider: 'openai',
            call_models: ['gpt-4o-mini'],
            fallback_used: false,
            execution_stage: 'workflow_dispatch',
            policy_stage: 'classifier',
            selection_mode: 'policy_primary_override',
            explicit_stage_model_override: true,
            explicit_stage_model_override_origin: 'policy_primary'
        });
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) =>
            seg.classList.contains('warning')
        );
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('warning')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(false);

        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton.textContent.trim()).toBe('gpt-4o-mini');
        expect(modelButton.getAttribute('data-keep-title')).toBe('true');
        expect(modelButton.title).toContain('Last execution status: Stage Override Active');
        expect(modelButton.title).toContain('Stage model override: explicit policy override');
        expect(modelButton.title).toContain('Execution stage: workflow_dispatch');
        expect(modelButton.title).toContain('Policy stage: classifier');
        expect(modelButton.title).toContain('Selection mode: policy_primary_override');
        expect(modelButton.title).toContain('Override origin: policy_primary');
    });

    test('preserves partial-readiness footer hover summary under global tooltip suppression', async () => {
        installFetchMock({ llmInfoOk: false });
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();
        await flushUiTicks();

        const footerContainer = document.querySelector('.footer-container');
        expect(footerContainer).toBeTruthy();
        expect(footerContainer.getAttribute('data-keep-title')).toBe('true');
        expect(footerContainer.title).toContain('Footer partially ready.');
    });

    test('uses the canonical capability snapshot when raw settings status disagrees', async () => {
        installFetchMock({
            settingsPayload: {
                resolved_llm: { provider: 'openai', model: 'gpt-5.4-mini' },
                workflow_capability_index: {
                    ready: true,
                    status: 'ready',
                    checked_at_utc: '2026-07-14T03:59:59Z',
                }
            },
            capabilityStatusPayload: {
                    ready: false,
                    workflow_discovery_available: false,
                    user_visible_severity: 'error',
                    status: 'building',
                    checked_at_utc: '2026-07-14T04:00:00Z',
                    summary: 'Workflow capability index still building.',
                    detail: 'Workflow discovery is waiting on the authoritative capability index to finish building.',
                    size: 0,
                    query_surface_ready: false,
                    namespace_state: {
                        status: 'missing_index',
                        detail: 'No persisted index exists for this namespace yet.'
                    }
                },
        });
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();
        const badge = await waitForWorkflowIndexBadge();
        const footerContainer = document.querySelector('.footer-container');
        const button = badge?.querySelector('.concept-footer-button');
        expect(badge).toBeTruthy();
        expect(badge.classList.contains('fatal')).toBe(true);
        expect(badge.textContent).toContain('Workflow Index');
        expect(badge.textContent).toContain('building');
        expect(button?.getAttribute('aria-label')).toBe('Open workflow capability index status');
        expect(button?.title).toContain('Represented workflow discovery is not fully available.');
        expect(button?.title).toContain('Namespace status: missing_index');
        expect(badge.dataset.checkedAtUtc).toBe('2026-07-14T04:00:00Z');
        expect(document.getElementById('modelInfoFooter').dataset.workflowCapabilityCheckedAtUtc)
            .toBe('2026-07-14T04:00:00Z');
        expect(footerContainer.classList.contains('footer-not-ready')).toBe(true);
        expect(footerContainer.title).toContain('workflow capability index');
    });

    test('keeps workflow index badge label readable on fatal status backgrounds', () => {
        const css = fs.readFileSync(stylesPath, 'utf8');
        const style = document.createElement('style');
        style.textContent = css;
        document.head.appendChild(style);
        document.body.innerHTML = `
            <span class="footer-segment workflow-index-status-badge fatal">
                <span class="footer-label-inline">Workflow Index: </span>
                <button class="concept-footer-button" type="button">error</button>
            </span>
        `;

        const badge = document.querySelector('.workflow-index-status-badge');
        const label = document.querySelector('.workflow-index-status-badge .footer-label-inline');

        expect(getComputedStyle(badge).color).toBe('rgb(255, 255, 255)');
        expect(getComputedStyle(label).color).toBe('rgb(255, 255, 255)');
    });
});
