/** @jest-environment jsdom */

const fs = require('fs');
const path = require('path');

import {
    __testOnly_addOrUpdateWorkflowPoolEntry,
    __testOnly_allowAndSaveWorkflowPoolEntry,
    __testOnly_applyCapabilityIndexStatusCard,
    __testOnly_applySharedRuntimeModelWriteAccess,
    __testOnly_buildPersistedLlmSelections,
    __testOnly_getScopedModelState,
    __testOnly_applySettingsActorRole,
    __testOnly_invalidateActorScopedCapabilityStatus,
    __testOnly_queueActorContextTransition,
    __testOnly_refreshSelectedOpenAiCostSummary,
    __testOnly_reloadScopedModelSettings,
    __testOnly_renderModelScopeOverview,
    __testOnly_restoreScopedPrimary,
    __testOnly_saveAllSettings,
    __testOnly_setModelScopeState,
    __testOnly_syncInitialScopedSelections,
    __testOnly_useBrowserModelAsScopedPrimary,
} from '../../src/frontend/web/von_interface/static/js/settingsPage.js';

const settingsTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/settings_tab.html'),
    'utf8'
);
const settingsPageSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/settingsPage.js'),
    'utf8'
);

describe('settings model scope and workflow pool controls', () => {
    beforeEach(() => {
        localStorage.clear();
        sessionStorage.clear();
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'ollama',
            openaiModel: 'gpt-5.6-luna',
            ollamaSelection: {
                value: 'http://127.0.0.1:11434:gemma4:latest',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
            },
        }));
        document.body.innerHTML = `
            <div id="browserChatModelSummary"></div>
            <div id="effectiveScopedModelSummary"></div>
            <div id="scopedPrimaryModelSummary"></div>
            <div id="sharedServerDefaultModelSummary"></div>
            <div id="workflowModelPoolList"></div>
            <div id="modelPoolStatusMessage"></div>
            <select id="modelPoolTargetScopeSelect">
                <option value="user">Current user</option>
                <option value="organisation">Current organisation</option>
            </select>
            <div id="modelPoolTargetScopeNote"></div>
            <button id="saveModelPoolButton"></button>
            <button id="useBrowserModelAsScopedPrimaryButton"></button>
            <button id="restoreScopedPrimaryButton"></button>
            <select id="openaiModelSelect"><option value="gpt-5.6-luna" selected>gpt-5.6-luna</option></select>
            <select id="openaiReasoningEffortSelect"><option value="low" selected>low</option></select>
            <input id="enableOpenAiPremiumToggle" type="checkbox" />
            <button id="testOpenAiModelButton"></button>
            <button id="addOpenAiToWorkflowPoolButton"></button>
            <div id="openaiModelEligibilityStatus"></div>
            <div id="openaiModelCostSummary"></div>
            <select id="globalModelSelect">
                <option value="http://127.0.0.1:11434:gemma4:latest"
                    data-host-url="http://127.0.0.1:11434"
                    data-model-name="gemma4:latest" selected>gemma4:latest</option>
            </select>
            <button id="addOllamaToWorkflowPoolButton"></button>
            <div id="settingsStatusMessage"></div>
        `;
        global.fetch = jest.fn();
        __testOnly_setModelScopeState({
            ready: false,
            actorReady: false,
            canManageOrganisation: false,
        });
    });

    test('shows arbitrary preserved alternatives while locking the scoped primary', () => {
        __testOnly_setModelScopeState({
            resolvedLlm: {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
                scope: 'user',
            },
            enabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest', host: 'http://127.0.0.1:11434' },
                { provider: 'openai', model: 'gpt-5.6-luna', model_parameters: { reasoning_effort: 'low' } },
                { provider: 'gemini', model: 'gemini-2.5-pro' },
            ],
            serverDefaultLlm: { provider: 'ollama', model: 'qwen3:8b' },
        });

        __testOnly_renderModelScopeOverview();

        const rows = Array.from(document.querySelectorAll('.settings-model-pool-entry'));
        expect(rows).toHaveLength(3);
        expect(rows[0].textContent).toContain('Primary · always allowed');
        expect(rows[0].querySelector('button')).toBeNull();
        expect(rows[1].textContent).toContain('gpt-5.6-luna');
        expect(rows[2].textContent).toContain('gemini-2.5-pro');
        expect(document.getElementById('browserChatModelSummary').textContent).toContain('gemma4:latest');
        expect(document.getElementById('sharedServerDefaultModelSummary').textContent).toContain('qwen3:8b');
    });

    test('uses the persisted effective allow list, not staged edits', () => {
        const eligibility = document.getElementById('openaiModelEligibilityStatus');
        const testButton = document.getElementById('testOpenAiModelButton');
        __testOnly_setModelScopeState({
            targetScope: 'user',
            resolvedLlm: null,
            enabledLlms: [],
            effectiveLlm: {
                provider: 'ollama',
                model: 'organisation-primary',
                scope: 'organisation',
            },
            effectiveEnabledLlms: [
                { provider: 'ollama', model: 'organisation-primary' },
                { provider: 'openai', model: 'gpt-5.6-luna' },
            ],
        });
        __testOnly_renderModelScopeOverview();
        expect(testButton.disabled).toBe(false);
        expect(document.getElementById('enableOpenAiPremiumToggle').disabled).toBe(false);

        __testOnly_setModelScopeState({
            resolvedLlm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
            enabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest' },
                { provider: 'openai', model: 'gpt-5.6-terra' },
            ],
        });
        __testOnly_renderModelScopeOverview();
        expect(testButton.disabled).toBe(true);
        expect(__testOnly_addOrUpdateWorkflowPoolEntry({
            provider: 'openai', model: 'gpt-5.6-luna',
            model_parameters: { reasoning_effort: 'low' },
        })).toBe(true);
        expect(document.getElementById('addOpenAiToWorkflowPoolButton').textContent)
            .toBe('Save allowed model now');
        expect(document.getElementById('addOpenAiToWorkflowPoolButton').disabled).toBe(false);
    });

    test('Allow checks the provider, persists once, retains existing entries and enables use after read-back', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#user-a' }));
        const primary = { provider: 'ollama', model: 'gemma4:latest', scope: 'user' };
        const old = { provider: 'openrouter', model: 'existing' };
        const added = { provider: 'openai', model: 'gpt-5.6-luna', model_parameters: { reasoning_effort: 'low' } };
        __testOnly_setModelScopeState({ resolvedLlm: primary, enabledLlms: [primary, old] });
        let stored;
        global.fetch.mockImplementation(async (url, options) => {
            if (url.includes('/models/inventory/')) return { ok: true, json: async () => ({ available: true, models: [{ ...added, available: true }] }) };
            if (options?.method === 'POST') {
                stored = JSON.parse(options.body);
                return { ok: true, json: async () => ({ resolved_llm: primary, enabled_llms: stored.enabled_llms }) };
            }
            return { ok: true, json: async () => ({ resolved_llm: primary, enabled_llms: stored?.enabled_llms || [] }) };
        });
        expect(await __testOnly_allowAndSaveWorkflowPoolEntry(added)).toBe(true);
        expect(stored.enabled_llms).toEqual(expect.arrayContaining([old, added]));
        expect(stored.active_llm.model).toBe(primary.model);
        expect(document.getElementById('testOpenAiModelButton').disabled).toBe(false);
        expect(document.getElementById('modelPoolStatusMessage').textContent).toContain('saved');
        expect((await __testOnly_reloadScopedModelSettings()).applied).toBe(true);
        expect(document.getElementById('testOpenAiModelButton').disabled).toBe(false);
        expect(stored.enabled_llms.filter(entry => entry.model === added.model)).toHaveLength(1);
    });

    test('unavailable providers do not mutate the staged pool and expose an actionable failure', async () => {
        __testOnly_setModelScopeState({ resolvedLlm: { provider: 'ollama', model: 'primary', scope: 'user' } });
        global.fetch.mockResolvedValue({ ok: true, json: async () => ({ available: false, error: 'OpenRouter: configure provider key.', models: [] }) });
        expect(await __testOnly_allowAndSaveWorkflowPoolEntry({ provider: 'openrouter', model: 'missing' })).toBe(false);
        expect(__testOnly_getScopedModelState().enabledAlternatives).toEqual([]);
        expect(document.getElementById('modelPoolStatusMessage').textContent).toContain('configure provider key');
        expect(global.fetch.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false);
    });

    test('failed persistence retains the edit and exposes the real error with a retry button', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#user-a' }));
        __testOnly_setModelScopeState({ resolvedLlm: { provider: 'ollama', model: 'primary', scope: 'user' } });
        const added = { provider: 'openai', model: 'gpt-5.6-luna', model_parameters: { reasoning_effort: 'low' } };
        global.fetch.mockImplementation(async (url) => url.includes('/models/inventory/')
            ? { ok: true, json: async () => ({ available: true, models: [{ ...added, available: true }] }) }
            : { ok: false, status: 500, json: async () => ({ message: 'Canonical persistence unavailable; retry.' }) });
        expect(await __testOnly_allowAndSaveWorkflowPoolEntry(added)).toBe(false);
        expect(document.getElementById('modelPoolStatusMessage').textContent).toContain('Canonical persistence unavailable');
        expect(document.getElementById('addOpenAiToWorkflowPoolButton').disabled).toBe(false);
        expect(document.getElementById('testOpenAiModelButton').disabled).toBe(true);
    });

    test('fetches and renders bounded actor-scoped cost evidence without inventing zero', async () => {
        __testOnly_setModelScopeState({
            targetScope: 'user',
            resolvedLlm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
            enabledLlms: [{ provider: 'ollama', model: 'gemma4:latest' }],
        });
        const projection = (status, amount = null) => ({
            schema_version: 'llm_model_turn_cost_summary.v1',
            status,
            model_identity: { provider: 'openai', model: 'gpt-5.6-luna' },
            average_attributed_cost_per_turn: { amount, currency: 'USD' },
            sample_window: {
                attributed_turn_count: 5,
                started_at_utc: '2026-08-01T00:00:00Z',
                ended_at_utc: '2026-08-05T00:00:00Z',
                limit: 100,
            },
            coverage: { priced_turn_count: 5, attributed_turn_count: 5 },
            pricing: {
                source: 'provider', version: '2026-08', effective_at_utc: '2026-08-01T00:00:00Z',
            },
        });
        global.fetch.mockResolvedValueOnce({
            ok: true,
            json: async () => projection('available', 0.05),
        });

        await __testOnly_refreshSelectedOpenAiCostSummary();

        const [url, options] = global.fetch.mock.calls[0];
        const parsed = new URL(url, 'http://von.test');
        expect(parsed.pathname).toBe('/api/settings/llm/cost_summary');
        expect(Object.fromEntries(parsed.searchParams.entries())).toEqual({
            provider: 'openai',
            model: 'gpt-5.6-luna',
        });
        expect(options.cache).toBe('no-store');
        expect(options.headers['X-Von-Window-Session']).toBeTruthy();
        const cost = document.getElementById('openaiModelCostSummary');
        expect(cost.textContent).toContain('US$0.05 per turn');
        expect(cost.textContent).toContain('5/5 priced · 2026-08-01–2026-08-05');
        expect(cost.textContent).toContain('pricing provider 2026-08 effective 2026-08-01');
        expect(cost.textContent).toContain('not provider billing');

        global.fetch.mockResolvedValueOnce({
            ok: true,
            json: async () => projection('available', 0.000012),
        });
        await __testOnly_refreshSelectedOpenAiCostSummary();
        expect(cost.textContent).toContain('US$0.0000120 per turn');
        expect(cost.textContent).not.toContain('US$0.0000 per turn');

        for (const [status, expected] of [
            ['partial', 'coverage is partial'],
            ['unavailable', 'estimate unavailable'],
            ['insufficient_coverage', 'Insufficient historical coverage'],
            ['available', 'estimate unavailable'],
        ]) {
            global.fetch.mockResolvedValueOnce({
                ok: true,
                json: async () => projection(status),
            });
            await __testOnly_refreshSelectedOpenAiCostSummary();
            expect(cost.textContent).toContain(expected);
            expect(cost.textContent).not.toContain('US$0.00');
        }

        global.fetch.mockResolvedValueOnce({
            ok: true,
            json: async () => ({ ...projection('available', 0.05), pricing: null }),
        });
        await __testOnly_refreshSelectedOpenAiCostSummary();
        expect(cost.textContent).toContain('estimate unavailable');
    });

    test('does not let an older model response replace the latest selection', async () => {
        __testOnly_setModelScopeState({
            resolvedLlm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
            enabledLlms: [{ provider: 'ollama', model: 'gemma4:latest' }],
        });
        let resolveOlder;
        global.fetch.mockImplementationOnce(() => new Promise((resolve) => { resolveOlder = resolve; }));
        const older = __testOnly_refreshSelectedOpenAiCostSummary();

        document.getElementById('openaiModelSelect').innerHTML = '<option value="gpt-5.6-terra" selected>gpt-5.6-terra</option>';
        global.fetch.mockResolvedValueOnce({
            ok: true,
            json: async () => ({
                schema_version: 'llm_model_turn_cost_summary.v1',
                status: 'partial',
                model_identity: { provider: 'openai', model: 'gpt-5.6-terra' },
            }),
        });
        await __testOnly_refreshSelectedOpenAiCostSummary();
        resolveOlder({
            ok: true,
            json: async () => ({
                schema_version: 'llm_model_turn_cost_summary.v1',
                status: 'available',
                model_identity: { provider: 'openai', model: 'gpt-5.6-luna' },
                average_attributed_cost_per_turn: { amount: 99, currency: 'USD' },
                sample_window: { attributed_turn_count: 100 },
            }),
        });
        await older;

        const cost = document.getElementById('openaiModelCostSummary');
        expect(cost.textContent).toContain('coverage is partial');
        expect(cost.textContent).not.toContain('US$99.00');
    });

    test('adds, updates, and removes alternatives only through explicit pool actions', () => {
        __testOnly_setModelScopeState({
            resolvedLlm: {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
                scope: 'user',
            },
            enabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest', host: 'http://127.0.0.1:11434' },
                { provider: 'gemini', model: 'gemini-2.5-pro' },
            ],
        });
        __testOnly_renderModelScopeOverview();

        document.querySelector('.settings-model-pool-entry button').click();
        expect(document.getElementById('workflowModelPoolList').textContent).not.toContain('gemini-2.5-pro');

        expect(__testOnly_addOrUpdateWorkflowPoolEntry({
            provider: 'openai',
            model: 'gpt-5.6-luna',
            model_parameters: { reasoning_effort: 'low' },
        })).toBe(true);
        expect(document.getElementById('workflowModelPoolList').textContent).toContain('gpt-5.6-luna');
        expect(document.getElementById('modelPoolStatusMessage').textContent).toContain('change pending');

        expect(__testOnly_addOrUpdateWorkflowPoolEntry({
            provider: 'openai',
            model: 'gpt-5.6-luna',
            model_parameters: { reasoning_effort: 'high' },
        })).toBe(true);

        const lunaRows = Array.from(document.querySelectorAll('.settings-model-pool-entry'))
            .filter((row) => row.textContent.includes('gpt-5.6-luna'));
        expect(lunaRows).toHaveLength(1);
        expect(lunaRows[0].textContent).toContain('high effort');
        expect(lunaRows[0].textContent).not.toContain('low effort');

        lunaRows[0].querySelector('button').click();
        expect(document.getElementById('workflowModelPoolList').textContent).not.toContain('gpt-5.6-luna');
    });

    test('preserves the former scoped primary when a new browser primary is selected', () => {
        const selections = __testOnly_buildPersistedLlmSelections({
            localModelPreference: {
                requestedLlm: { provider: 'openai', model: 'gpt-5.6-luna' },
            },
            primary: { provider: 'openai', model: 'gpt-5.6-luna' },
            formerPrimary: {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
            },
            enabledAlternatives: [{ provider: 'gemini', model: 'gemini-2.5-pro' }],
            explicitlyRemovedKeys: new Set(),
        });

        expect(selections).toEqual([
            { provider: 'openai', model: 'gpt-5.6-luna' },
            {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
            },
            { provider: 'gemini', model: 'gemini-2.5-pro' },
        ]);
    });

    test('shows the former primary as removable only after an explicit primary change', () => {
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.6-luna',
        }));
        __testOnly_setModelScopeState({
            resolvedLlm: {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
                scope: 'user',
            },
            enabledLlms: [
                { provider: 'ollama', model: 'gemma4:latest', host: 'http://127.0.0.1:11434' },
            ],
        });

        expect(__testOnly_useBrowserModelAsScopedPrimary()).toBe(true);

        __testOnly_renderModelScopeOverview();

        const rows = Array.from(document.querySelectorAll('.settings-model-pool-entry'));
        expect(rows[0].textContent).toContain('gpt-5.6-luna');
        expect(rows[0].textContent).toContain('explicit save required');
        expect(rows[1].textContent).toContain('gemma4:latest');
        rows[1].querySelector('button').click();
        expect(document.getElementById('workflowModelPoolList').textContent).not.toContain('gemma4:latest');
        expect(__testOnly_buildPersistedLlmSelections({
            primary: { provider: 'openai', model: 'gpt-5.6-luna' },
            formerPrimary: {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
            },
            enabledAlternatives: [],
        })).toEqual([{ provider: 'openai', model: 'gpt-5.6-luna' }]);
        expect(__testOnly_restoreScopedPrimary()).toBe(true);
        expect(__testOnly_getScopedModelState().stagedPrimaryLlm).toMatchObject({
            provider: 'ollama',
            model: 'gemma4:latest',
        });
    });

    test('shows inherited organisation state without copying it into a new user target', () => {
        __testOnly_setModelScopeState({
            targetScope: 'user',
            resolvedLlm: null,
            enabledLlms: [],
            effectiveLlm: {
                provider: 'ollama',
                model: 'organisation-primary',
                scope: 'organisation',
            },
            effectiveEnabledLlms: [
                { provider: 'ollama', model: 'organisation-primary' },
                { provider: 'openai', model: 'organisation-alternative' },
            ],
        });

        __testOnly_renderModelScopeOverview();

        expect(document.getElementById('effectiveScopedModelSummary').textContent)
            .toContain('inherited from organisation scope');
        expect(document.getElementById('scopedPrimaryModelSummary').textContent)
            .toContain('inherited; saving creates a user override');
        expect(document.getElementById('modelPoolTargetScopeNote').textContent)
            .toContain('organisation allowed models are not copied');
        expect(document.getElementById('workflowModelPoolList').textContent)
            .not.toContain('organisation-alternative');
        expect(__testOnly_buildPersistedLlmSelections()).toEqual([
            {
                provider: 'ollama',
                model: 'gemma4:latest',
                host: 'http://127.0.0.1:11434',
            },
        ]);
    });

    test('organisation target is available only to an admin or owner with a current organisation', () => {
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#org-a' }));
        __testOnly_setModelScopeState({
            targetScope: 'organisation',
            canManageOrganisation: false,
        });
        expect(document.getElementById('modelPoolTargetScopeSelect').value).toBe('user');
        expect(document.querySelector('#modelPoolTargetScopeSelect option[value="organisation"]').disabled)
            .toBe(true);

        __testOnly_setModelScopeState({
            targetScope: 'organisation',
            canManageOrganisation: true,
        });
        expect(document.getElementById('modelPoolTargetScopeSelect').value).toBe('organisation');
        expect(document.querySelector('#modelPoolTargetScopeSelect option[value="organisation"]').disabled)
            .toBe(false);
    });

    test('only the latest actor-scoped reload can repopulate state and enable Save', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#user-b' }));
        __testOnly_setModelScopeState({ ready: true, actorReady: true });
        const pending = new Map();
        const fetchFn = jest.fn((url) => new Promise((resolve) => pending.set(url, resolve)));

        const first = __testOnly_reloadScopedModelSettings({
            actorContext: { userConceptId: '#V#user-a', organisationConceptId: '#V#org' },
            fetchFn,
        });
        const second = __testOnly_reloadScopedModelSettings({
            actorContext: { userConceptId: '#V#user-b', organisationConceptId: '#V#org' },
            fetchFn,
        });
        expect(document.getElementById('saveModelPoolButton').disabled).toBe(true);
        expect(__testOnly_getScopedModelState().ready).toBe(false);
        await expect(__testOnly_saveAllSettings({ includeChatModels: true }))
            .rejects.toThrow('finish loading');
        expect(global.fetch).not.toHaveBeenCalled();

        // Allow the asynchronous window-session header preparation to complete.
        await new Promise(resolve => setTimeout(resolve, 0));
        pending.get('/api/settings/?user_concept_id=%23V%23user-b&organisation_concept_id=%23V%23org&model_scope=user')({
            ok: true,
            json: async () => ({
                resolved_llm: { provider: 'openai', model: 'newer-model', scope: 'user' },
                enabled_llms: [{ provider: 'openai', model: 'newer-model' }],
                effective_llm: { provider: 'openai', model: 'newer-model', scope: 'user' },
                effective_enabled_llms: [{ provider: 'openai', model: 'newer-model' }],
                selected_model_scope: 'user',
            }),
        });
        await second;
        expect(document.getElementById('saveModelPoolButton').disabled).toBe(false);
        expect(document.getElementById('scopedPrimaryModelSummary').textContent).toContain('newer-model');

        pending.get('/api/settings/?user_concept_id=%23V%23user-a&organisation_concept_id=%23V%23org&model_scope=user')({
            ok: true,
            json: async () => ({
                resolved_llm: { provider: 'ollama', model: 'stale-model', scope: 'user' },
                enabled_llms: [{ provider: 'ollama', model: 'stale-model' }],
                selected_model_scope: 'user',
            }),
        });
        const staleResult = await first;
        expect(staleResult).toMatchObject({ applied: false, stale: true });
        expect(document.getElementById('scopedPrimaryModelSummary').textContent).toContain('newer-model');
        expect(__testOnly_getScopedModelState()).toMatchObject({
            ready: true,
            resolvedLlm: { provider: 'openai', model: 'newer-model' },
        });
    });

    test('organisation transition commits the authenticated server user and organisation before scoped GET', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#new-user',
            name: 'New User',
        }));
        const order = [];
        let releaseUserCommit;
        const userCommit = new Promise((resolve) => { releaseUserCommit = resolve; });
        const setUserConceptFn = jest.fn(async () => {
            order.push('set-user:start');
            await userCommit;
            order.push('set-user:done');
            return { namespace: '#V#new-user' };
        });
        const switchOrganisationFn = jest.fn(async () => {
            order.push('set-organisation');
            return { namespace: '#V#new-user@new-org' };
        });
        const reloadScopedModelsFn = jest.fn(async () => {
            order.push('scoped-get');
            return { applied: true, stale: false };
        });

        const transition = __testOnly_queueActorContextTransition({
            snapshot: { user: { concept_id: '#V#new-user' }, organisation: null },
            requestedOrganisation: { concept_id: '#V#new-org', name: 'New Org' },
            setUserConceptFn,
            switchOrganisationFn,
            reloadScopedModelsFn,
            rollbackActorFn: jest.fn(),
            refreshRagStatusFn: jest.fn(),
        });

        expect(__testOnly_getScopedModelState()).toMatchObject({ ready: false, actorReady: false });
        expect(order).toEqual([]);
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(order).toEqual(['set-user:start']);
        expect(reloadScopedModelsFn).not.toHaveBeenCalled();

        releaseUserCommit();
        await transition;
        expect(order).toEqual([
            'set-user:start',
            'set-user:done',
            'set-organisation',
            'scoped-get',
        ]);
    });

    test('failed actor switch rolls back browser context and remains fail closed', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#old-user' }));
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#old-org' }));
        const rollbackActorFn = jest.fn(async () => []);
        const reloadScopedModelsFn = jest.fn();

        const result = await __testOnly_queueActorContextTransition({
            snapshot: {
                user: { concept_id: '#V#old-user' },
                organisation: { concept_id: '#V#old-org' },
                organisationSelectedIndex: -1,
            },
            requestedOrganisation: { concept_id: '#V#new-org' },
            setUserConceptFn: jest.fn(async () => ({ namespace: '#V#new-user' })),
            switchOrganisationFn: jest.fn(async () => { throw new Error('organisation commit failed'); }),
            reloadScopedModelsFn,
            rollbackActorFn,
            refreshRagStatusFn: jest.fn(),
        });

        expect(result).toMatchObject({ applied: false, stale: false });
        expect(rollbackActorFn).toHaveBeenCalled();
        expect(reloadScopedModelsFn).not.toHaveBeenCalled();
        expect(__testOnly_getScopedModelState()).toMatchObject({ ready: false, actorReady: false });
        expect(JSON.parse(sessionStorage.getItem('von_current_user')).concept_id).toBe('#V#old-user');
        expect(document.getElementById('saveModelPoolButton').disabled).toBe(true);
    });

    test.each([null, '#V#lab'])('background settings preserves an established window actor in %s', async (organisationId) => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#alice' }));
        if (organisationId) sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: organisationId }));
        const setUserConcept = jest.fn();
        const switchOrganisationFn = jest.fn();
        const result = await __testOnly_syncInitialScopedSelections({
            readSessionContext: async () => ({ authenticated: true, context_source: 'window_session', user_id: '#V#alice', organisation_id: organisationId, namespace: 'verified-namespace', role: 'member' }),
            setUserConcept, switchOrganisationFn, refreshRagStatus: jest.fn(),
        });
        expect(result).toMatchObject({ applied: true, namespace: 'verified-namespace', role: 'member' });
        expect(setUserConcept).not.toHaveBeenCalled();
        expect(switchOrganisationFn).not.toHaveBeenCalled();
    });

    test.each([
        { context_source: 'flask_session', user_id: '#V#alice' },
        { context_source: 'window_session', user_id: '#V#other' },
        { context_source: 'window_session', user_id: '#V#alice', organisation_id: '#V#other-org' },
    ])('initial settings still binds a missing or different window actor: %j', async (context) => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#alice' }));
        const setUserConcept = jest.fn(async () => ({ namespace: '#V#alice' }));
        const switchOrganisationFn = jest.fn(async () => ({ namespace: '#V#alice' }));
        await __testOnly_syncInitialScopedSelections({
            readSessionContext: async () => ({ authenticated: true, ...context }),
            setUserConcept, switchOrganisationFn, refreshRagStatus: jest.fn(),
        });
        expect(setUserConcept).toHaveBeenCalledWith('#V#alice');
        expect(switchOrganisationFn).toHaveBeenCalledWith(null, null);
    });

    test('failed initial actor commit restores committed context and leaves Save disabled', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#restored-user',
            name: 'Restored User',
        }));
        localStorage.setItem('von_current_user', JSON.stringify({
            concept_id: '#V#restored-user',
            name: 'Restored User',
        }));
        document.body.insertAdjacentHTML('beforeend', `
            <select id="currentOrganisationSelect">
                <option data-concept-id="#V#restored-org" selected>Restored Org</option>
            </select>
        `);
        __testOnly_setModelScopeState({ ready: true, actorReady: true });
        const setUserConcept = jest.fn()
            .mockRejectedValueOnce(new Error('user commit failed'))
            .mockResolvedValueOnce({ namespace: '#V#committed-user' });
        const switchOrganisationFn = jest.fn().mockResolvedValue({
            namespace: '#V#committed-user@committed-org',
            role: 'member',
        });

        await expect(__testOnly_syncInitialScopedSelections({
            setUserConcept,
            switchOrganisationFn,
            refreshRagStatus: jest.fn(),
            committedUser: { concept_id: '#V#committed-user', name: 'Committed User' },
            committedOrganisation: { concept_id: '#V#committed-org', name: 'Committed Org' },
        })).rejects.toThrow('Scoped model saving remains disabled');

        expect(JSON.parse(sessionStorage.getItem('von_current_user'))).toMatchObject({
            concept_id: '#V#committed-user',
        });
        expect(JSON.parse(sessionStorage.getItem('von_current_org'))).toMatchObject({
            concept_id: '#V#committed-org',
        });
        expect(__testOnly_getScopedModelState()).toMatchObject({
            actorReady: false,
            ready: false,
        });
        expect(document.getElementById('saveModelPoolButton').disabled).toBe(true);
    });

    test('actor role changes update organisation and shared-model write controls', () => {
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#org-a' }));
        document.body.insertAdjacentHTML('beforeend', `
            <p id="sharedRuntimeModelWriteAccessNote"></p>
            <button id="saveRuntimeModelSettingsButton"></button>
            <input id="newOllamaHostUrl" />
            <button id="addOllamaHostButton"></button>
        `);
        __testOnly_setModelScopeState({ ready: true, actorReady: true });

        __testOnly_applySettingsActorRole('owner');
        expect(document.querySelector('#modelPoolTargetScopeSelect option[value="organisation"]').disabled)
            .toBe(false);
        expect(document.getElementById('saveRuntimeModelSettingsButton').disabled).toBe(false);
        expect(document.getElementById('addOllamaHostButton').disabled).toBe(false);

        __testOnly_applySettingsActorRole('member');
        expect(document.querySelector('#modelPoolTargetScopeSelect option[value="organisation"]').disabled)
            .toBe(true);
        expect(document.getElementById('saveRuntimeModelSettingsButton').disabled).toBe(true);
        expect(document.getElementById('addOllamaHostButton').disabled).toBe(true);
    });

    test('actor changes clear the previous capability snapshot immediately', () => {
        document.body.insertAdjacentHTML('beforeend', `
            <section id="workflowCapabilityIndexStatusCard">
                <p id="workflowCapabilityIndexStatusSummary"></p>
                <p id="workflowCapabilityIndexStatusDetail"></p>
            </section>
        `);
        const card = document.getElementById('workflowCapabilityIndexStatusCard');

        __testOnly_applyCapabilityIndexStatusCard({
            ready: true,
            status: 'ready',
            warning_level: 'ok',
            checked_at_utc: '2026-07-14T04:00:00Z',
            summary: 'Actor A capability index is ready.',
            detail: 'Actor A can discover represented workflows.',
        });
        expect(card.dataset.checkedAtUtc).toBe('2026-07-14T04:00:00Z');
        expect(document.getElementById('workflowCapabilityIndexStatusSummary').textContent)
            .toContain('Actor A');

        __testOnly_invalidateActorScopedCapabilityStatus();

        expect(card.dataset.status).toBe('unknown');
        expect(card.dataset.checkedAtUtc).toBe('');
        expect(document.getElementById('workflowCapabilityIndexStatusSummary').textContent)
            .toBe('Workflow capability index status unavailable.');
        expect(document.getElementById('workflowCapabilityIndexStatusDetail').textContent)
            .toBe('No authoritative workflow capability status is currently available.');
    });

    test('model-pool Save sends no unrelated general or runtime settings', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#user-a' }));
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'openai',
            openaiModel: 'gpt-5.6-luna',
        }));
        __testOnly_setModelScopeState({
            resolvedLlm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
            enabledLlms: [{ provider: 'ollama', model: 'gemma4:latest' }],
        });
        global.fetch.mockResolvedValue({
            ok: true,
            json: async () => ({
                resolved_llm: { provider: 'ollama', model: 'gemma4:latest', scope: 'user' },
                enabled_llms: [{ provider: 'ollama', model: 'gemma4:latest' }],
            }),
        });

        expect(await __testOnly_saveAllSettings({ includeChatModels: true })).toBe(true);
        const payload = JSON.parse(global.fetch.mock.calls[0][1].body);
        expect(Object.keys(payload).sort()).toEqual(['active_llm', 'enabled_llms']);
        expect(payload.active_llm).toMatchObject({ provider: 'ollama', model: 'gemma4:latest' });
        expect(payload.enabled_llms).toContainEqual({ provider: 'ollama', model: 'gemma4:latest' });
    });

    test('pool-only edit retains an organisation primary that differs from browser chat', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#user-a' }));
        sessionStorage.setItem('von_current_org', JSON.stringify({ concept_id: '#V#org-a' }));
        __testOnly_setModelScopeState({
            targetScope: 'organisation',
            canManageOrganisation: true,
            resolvedLlm: { provider: 'ollama', model: 'qwen3:8b', scope: 'organisation' },
            effectiveLlm: { provider: 'ollama', model: 'qwen3:8b', scope: 'organisation' },
            enabledLlms: [{ provider: 'ollama', model: 'qwen3:8b' }],
        });
        expect(__testOnly_addOrUpdateWorkflowPoolEntry({
            provider: 'openai',
            model: 'gpt-5.6-luna',
            model_parameters: { reasoning_effort: 'high' },
        })).toBe(true);
        global.fetch.mockResolvedValue({
            ok: true,
            json: async () => ({
                resolved_llm: { provider: 'ollama', model: 'qwen3:8b', scope: 'organisation' },
                enabled_llms: [
                    { provider: 'ollama', model: 'qwen3:8b' },
                    {
                        provider: 'openai',
                        model: 'gpt-5.6-luna',
                        model_parameters: { reasoning_effort: 'high' },
                    },
                ],
            }),
        });

        expect(await __testOnly_saveAllSettings({ includeChatModels: true })).toBe(true);
        const payload = JSON.parse(global.fetch.mock.calls[0][1].body);
        expect(payload.active_llm).toMatchObject({
            provider: 'ollama',
            model: 'qwen3:8b',
            scope: 'organisation',
            concept_id: '#V#org-a',
        });
        expect(payload.enabled_llms).toEqual([
            { provider: 'ollama', model: 'qwen3:8b' },
            {
                provider: 'openai',
                model: 'gpt-5.6-luna',
                model_parameters: { reasoning_effort: 'high' },
            },
        ]);
    });

    test('shared runtime Save sends no actor-scoped or general settings', async () => {
        document.body.insertAdjacentHTML('beforeend', `
            <select id="serverDefaultLlmProvider"><option value="ollama" selected>Ollama</option></select>
            <input id="serverDefaultLlmModel" value="qwen3:8b" />
            <input id="serverDefaultLlmHost" value="http://127.0.0.1:11434" />
            <select id="ragEmbedderMode"><option value="inherit" selected>Inherit</option></select>
            <select id="ragLlmMode"><option value="disabled" selected>Disabled</option></select>
        `);
        global.fetch.mockResolvedValue({
            ok: true,
            json: async () => ({ server_default_llm: { provider: 'ollama', model: 'qwen3:8b' } }),
        });

        expect(await __testOnly_saveAllSettings({ includeRuntimeModels: true })).toBe(true);
        const payload = JSON.parse(global.fetch.mock.calls[0][1].body);
        expect(Object.keys(payload).sort()).toEqual(['rag_embedder', 'rag_llm', 'server_default_llm']);
        expect(payload).not.toHaveProperty('active_llm');
        expect(payload).not.toHaveProperty('openai_api_key_env_var');
    });

    test('shared server and RAG controls are visibly read-only for non-admin sessions', () => {
        document.body.insertAdjacentHTML('beforeend', `
            <p id="sharedRuntimeModelWriteAccessNote"></p>
            <select id="serverDefaultLlmProvider"></select>
            <input id="serverDefaultLlmModel" />
            <input id="serverDefaultLlmHost" />
            <select id="ragEmbedderMode"><option value="inherit" selected>Inherit</option></select>
            <select id="ragEmbedderProvider"></select>
            <input id="ragEmbedderModel" />
            <input id="ragEmbedderHost" />
            <select id="ragLlmMode"><option value="inherit" selected>Inherit</option></select>
            <select id="ragLlmProvider"></select>
            <input id="ragLlmModel" />
            <input id="ragLlmHost" />
            <button id="saveRuntimeModelSettingsButton"></button>
        `);

        __testOnly_applySharedRuntimeModelWriteAccess(false);
        expect(document.getElementById('saveRuntimeModelSettingsButton').disabled).toBe(true);
        expect(document.getElementById('serverDefaultLlmModel').disabled).toBe(true);
        expect(document.getElementById('sharedRuntimeModelWriteAccessNote').textContent)
            .toContain('only an administrator or organisation owner');

        __testOnly_applySharedRuntimeModelWriteAccess(true);
        expect(document.getElementById('saveRuntimeModelSettingsButton').disabled).toBe(false);
        expect(document.getElementById('serverDefaultLlmModel').disabled).toBe(false);
        expect(document.getElementById('sharedRuntimeModelWriteAccessNote').dataset.writable).toBe('true');
    });

    test('describes inherited RAG resolution as a shared server concern', () => {
        expect(settingsTemplate.match(/Inherit server default/g)).toHaveLength(2);
        expect(settingsTemplate).not.toContain('Inherit chat default');
        expect(settingsTemplate).toContain('Effective shared RAG LLM: —');
        expect(settingsTemplate).toContain('Save scoped primary &amp; allowed models');
        expect(settingsTemplate).toContain('Scoped allowed models');
        expect(settingsTemplate).toContain('Blank fields retain the saved');
        expect(settingsTemplate).not.toContain('uses the current browser chat selection as the server default');
        expect(settingsPageSource).toContain("'Effective shared RAG LLM'");
        expect(settingsPageSource).toContain("if (token === 'server_default_llm') return 'server default';");
    });
});
