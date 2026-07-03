/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
    postJson: jest.fn()
}));

const {
    __testOnly_applyCapabilityIndexStatusCard,
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

    test('renders unavailable workflow capability index as user-visible error', () => {
        __testOnly_applyCapabilityIndexStatusCard({
            ready: false,
            status: 'building',
            warning_level: 'warning',
            user_visible_severity: 'error',
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
        expect(document.getElementById('workflowCapabilityIndexStatusSummary').textContent)
            .toContain('still building');
        expect(document.getElementById('workflowCapabilityIndexStatusDetail').textContent)
            .toContain('No persisted index exists');
    });
});
