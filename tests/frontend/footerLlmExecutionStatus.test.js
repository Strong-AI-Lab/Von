/** @jest-environment jsdom */

const domUtilsPath = '../../src/frontend/web/von_interface/static/js/domUtils.js';
const suppressTooltipsPath = '../../src/frontend/web/von_interface/static/js/suppressTooltips.js';

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
        authPayload = { authenticated: true, email: 'researcher@example.test' }
    } = options;
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
        expect(modelButton.getAttribute('aria-label')).toBe('Open language model settings');
        expect(modelButton.title).toContain('Configured status: Unselected');
        expect(modelButton.title).toContain('No usable model configured: premium model use is disabled and no Ollama model is selected.');

        const llmInfoCalls = global.fetch.mock.calls.filter(([url]) => {
            const rawUrl = typeof url === 'string' ? url : (url?.url || String(url));
            return new URL(rawUrl, 'http://localhost').pathname === '/api/settings/llm/info';
        });
        expect(llmInfoCalls).toHaveLength(0);
    });

    test('turns footer red and shows actual fallback model plus reason', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gpt-5.4-mini',
            actual_model: 'granite3.3:2b',
            actual_provider: 'ollama',
            call_models: ['gpt-5.4-mini', 'granite3.3:2b'],
            fallback_used: true,
            primary_failure_kind: 'quota_exhausted',
            primary_failure_reason: 'OpenAI quota exhausted (insufficient_quota): exceeded your current quota.',
            warnings: ['Backend error: OpenAI quota exhausted (insufficient_quota): exceeded your current quota.']
        };
        document.dispatchEvent(new CustomEvent('von:latestLlmExecutionTelemetryUpdated', {
            detail: window.__vonLatestLlmExecutionTelemetry
        }));
        await flushUiTicks();

        const modelSegment = await waitForModelSegment((seg) => (
            seg.classList.contains('fatal')
            && seg.querySelector('.concept-footer-button')?.textContent?.trim() === 'granite3.3:2b'
        ));
        expect(modelSegment).toBeTruthy();
        expect(modelSegment.classList.contains('llm-status-badge')).toBe(true);
        expect(modelSegment.classList.contains('fatal')).toBe(true);

        const modelButton = modelSegment.querySelector('.concept-footer-button');
        expect(modelButton.textContent.trim()).toBe('granite3.3:2b');
        expect(modelButton.getAttribute('data-keep-title')).toBe('true');
        expect(modelButton.title).toContain('Configured model: gpt-5.4-mini');
        expect(modelButton.title).toContain('Executed provider: ollama');
        expect(modelButton.title).toContain('Executed model: granite3.3:2b');
        expect(modelButton.title).toContain('Failure kind: quota_exhausted');
        expect(modelButton.title).toContain('Failure reason: OpenAI quota exhausted (insufficient_quota): exceeded your current quota.');
    });

    test('does not turn footer red for OpenAI alias resolution (dated snapshot)', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini-2026-03-17',
            actual_provider: 'openai',
            call_models: ['gpt-5.4-mini-2026-03-17'],
            fallback_used: false,
        };
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

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini',
            actual_provider: 'openai',
            call_models: ['gpt-5.4-mini'],
            fallback_used: false,
            primary_failure_reason: null,
            warnings: [
                'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
            ]
        };
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

    test('shows warning (not fatal) when fallback was used but final model succeeded', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-5.4-mini-2026-03-17',
            actual_provider: 'openai',
            call_models: ['granite3.3:2b', 'gpt-5.4-mini-2026-03-17'],
            fallback_used: true,
        };
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

    test('shows fatal when actual model genuinely differs from requested', async () => {
        const { setModelInfoFooterText } = require(domUtilsPath);

        await setModelInfoFooterText();

        window.__vonLatestLlmExecutionTelemetry = {
            requested_model: 'gpt-5.4-mini',
            actual_model: 'gpt-4o-mini',
            actual_provider: 'openai',
            call_models: ['gpt-4o-mini'],
            fallback_used: false,
        };
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

        window.__vonLatestLlmExecutionTelemetry = {
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
        };
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
});
