/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn()
}));

const {
    __testOnly_applyCapabilityIndexStatusCard,
    __testOnly_invalidateActorScopedCapabilityStatus,
    __testOnly_refreshRuntimeModelStatus,
    __testOnly_resetRuntimeModelStatusCache
} = require('../../src/frontend/web/von_interface/static/js/settingsPage.js');

describe('settings runtime model status polling', () => {
    beforeEach(() => {
        __testOnly_resetRuntimeModelStatusCache();
        document.body.innerHTML = `
            <div id="serverDefaultLlmSummary"></div>
            <div id="ragEmbedderSummary"></div>
            <div id="ragLlmSummary"></div>
            <div id="workflowCapabilityIndexStatusCard"></div>
            <div id="workflowCapabilityIndexStatusSummary"></div>
            <div id="workflowCapabilityIndexStatusDetail"></div>
        `;
    });

    afterEach(() => {
        __testOnly_resetRuntimeModelStatusCache();
        delete global.fetch;
    });

    test('coalesces concurrent runtime and capability status refreshes', async () => {
        let resolveRuntime;
        let resolveCapability;
        global.fetch = jest.fn((url) => {
            if (String(url).startsWith('/admin/rag_runtime')) {
                return new Promise((resolve) => { resolveRuntime = resolve; });
            }
            if (String(url).startsWith('/api/workflows/capability-index/status')) {
                return new Promise((resolve) => { resolveCapability = resolve; });
            }
            throw new Error(`Unexpected URL ${url}`);
        });

        const first = __testOnly_refreshRuntimeModelStatus();
        const second = __testOnly_refreshRuntimeModelStatus();

        await Promise.resolve();
        expect(global.fetch).toHaveBeenCalledTimes(2);

        resolveRuntime({
            ok: true,
            json: async () => ({
                success: true,
                runtime_configuration: {
                    embedder_resolution: { status: 'disabled' },
                    llm_resolution: { status: 'disabled' }
                }
            })
        });
        resolveCapability({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Workflow capability index ready.'
            })
        });

        await Promise.all([first, second]);
        expect(global.fetch).toHaveBeenCalledTimes(2);
    });

    test('skips quiet runtime model refreshes during cooldown but keeps manual refresh fresh', async () => {
        global.fetch = jest.fn((url) => Promise.resolve({
            ok: true,
            json: async () => {
                if (String(url).startsWith('/admin/rag_runtime')) {
                    return {
                        success: true,
                        runtime_configuration: {
                            embedder_resolution: { status: 'disabled' },
                            llm_resolution: { status: 'disabled' }
                        }
                    };
                }
                return {
                    ready: true,
                    status: 'ready',
                    summary: 'Workflow capability index ready.'
                };
            }
        }));

        await __testOnly_refreshRuntimeModelStatus();
        await __testOnly_refreshRuntimeModelStatus();
        await __testOnly_refreshRuntimeModelStatus({ force: true });

        expect(global.fetch.mock.calls.map((call) => call[0])).toEqual([
            '/admin/rag_runtime?namespace=workflow_capabilities',
            '/api/workflows/capability-index/status',
            '/admin/rag_runtime?namespace=workflow_capabilities&nocache=1',
            '/api/workflows/capability-index/status?nocache=1'
        ]);
    });

    test('actor switching starts fresh status requests and ignores the old completion cooldown', async () => {
        let now = 0;
        jest.spyOn(Date, 'now').mockImplementation(() => now);
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor-a' }));
        const pending = [];
        global.fetch = jest.fn((url) => new Promise((resolve) => {
            pending.push({ url: String(url), resolve });
        }));

        const actorARefresh = __testOnly_refreshRuntimeModelStatus();
        await Promise.resolve();
        expect(global.fetch).toHaveBeenCalledTimes(2);

        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor-b' }));
        __testOnly_invalidateActorScopedCapabilityStatus();
        const actorBRefresh = __testOnly_refreshRuntimeModelStatus({ force: true });
        await Promise.resolve();
        expect(global.fetch).toHaveBeenCalledTimes(4);

        now = 1000;
        pending[2].resolve({
            ok: true,
            json: async () => ({
                success: true,
                runtime_configuration: {
                    embedder_resolution: { status: 'disabled' },
                    llm_resolution: { status: 'disabled' },
                },
            }),
        });
        pending[3].resolve({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Actor B capability index ready.',
                checked_at_utc: '2026-07-14T04:00:10Z',
            }),
        });
        await expect(actorBRefresh).resolves.toMatchObject({ runtime: true, capability: true });

        now = 2000;
        pending[0].resolve({
            ok: true,
            json: async () => ({
                success: true,
                runtime_configuration: {
                    embedder_resolution: { status: 'error' },
                    llm_resolution: { status: 'error' },
                },
            }),
        });
        pending[1].resolve({
            ok: true,
            json: async () => ({
                ready: false,
                status: 'building',
                summary: 'Actor A capability index building.',
                checked_at_utc: '2026-07-14T04:00:20Z',
            }),
        });
        await expect(actorARefresh).resolves.toBeNull();

        now = 31501;
        const actorBPostCooldownRefresh = __testOnly_refreshRuntimeModelStatus();
        await Promise.resolve();
        expect(global.fetch).toHaveBeenCalledTimes(6);

        pending[4].resolve({ ok: false, json: async () => ({}) });
        pending[5].resolve({ ok: false, json: async () => ({}) });
        await actorBPostCooldownRefresh;
    });

    test('renders unavailable workflow capability index as user-visible error', () => {
        __testOnly_applyCapabilityIndexStatusCard({
            ready: false,
            status: 'building',
            warning_level: 'warning',
            user_visible_severity: 'error',
            checked_at_utc: '2026-07-14T04:00:00Z',
            summary: 'Workflow capability index still building.',
            detail: 'Workflow discovery is waiting on the authoritative capability index.',
            namespace_state: {
                detail: 'No persisted index exists for this namespace yet.'
            }
        });

        const card = document.getElementById('workflowCapabilityIndexStatusCard');
        expect(card.dataset.status).toBe('building');
        expect(card.dataset.warningLevel).toBe('error');
        expect(card.dataset.userVisibleSeverity).toBe('error');
        expect(card.dataset.checkedAtUtc).toBe('2026-07-14T04:00:00Z');
        expect(document.getElementById('workflowCapabilityIndexStatusSummary').textContent)
            .toContain('still building');
        expect(document.getElementById('workflowCapabilityIndexStatusDetail').textContent)
            .toContain('No persisted index exists');
    });
});
