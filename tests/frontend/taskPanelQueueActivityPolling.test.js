/** @jest-environment jsdom */

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const apiServiceModulePath = '../../src/frontend/web/von_interface/static/js/apiService.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    getUserContext: jest.fn(() => ({
        user_id: '#V#alice',
        org_id: '#V#research_lab',
    })),
    patchJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

function queueResponse(status, updatedAt) {
    return {
        success: true,
        items: [{
            queue_id: 'queue-observed',
            status,
            task_concept_id: '#V#task_observed',
            task_execution_concept_id: '#V#execution_observed',
            session_id: 'session-observed',
            session_name: 'Observed conversation',
            updated_at: updatedAt,
        }],
        recent_failed_items: [],
    };
}

describe('global task queue activity polling', () => {
    beforeEach(() => {
        jest.resetModules();
        jest.useFakeTimers();
        jest.spyOn(console, 'debug').mockImplementation(() => {});
        document.body.innerHTML = `
            <div id="globalTasksTab" class="tab-content active">
                <div id="globalTasksContainer"></div>
            </div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;
        document.body.dataset.activeTab = 'globalTasksTab';
    });

    afterEach(async () => {
        document.body.dataset.activeTab = 'chatTab';
        document.dispatchEvent(new CustomEvent('von:tab-activated', {
            detail: { tabId: 'chatTab' },
        }));
        await jest.runOnlyPendingTimersAsync();
        jest.useRealTimers();
        jest.restoreAllMocks();
    });

    test('refreshes observer-only activity while open and stops after leaving the tab', async () => {
        const { getJson } = require(apiServiceModulePath);
        let queueReads = 0;
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve({ task_types: [], task_sources: [], defaults: {} });
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({ organisations: [] });
            }
            if (url === '/von/api/chat_prompt_queue') {
                queueReads += 1;
                return Promise.resolve(queueReads === 1
                    ? queueResponse('queued', '2026-09-01T12:00:00Z')
                    : queueResponse('in_progress', '2026-09-01T12:00:05Z'));
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({
                    tasks: [],
                    count: 0,
                    offset: 0,
                    has_more: false,
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();

        expect(queueReads).toBe(1);
        expect(document.querySelector('#globalTaskQueueActivity').textContent)
            .toContain('queued');

        await jest.advanceTimersByTimeAsync(4999);
        expect(queueReads).toBe(1);
        await jest.advanceTimersByTimeAsync(1);
        expect(queueReads).toBe(2);
        expect(document.querySelector('#globalTaskQueueActivity').textContent)
            .toContain('in progress');

        document.body.dataset.activeTab = 'chatTab';
        document.querySelector('#globalTasksTab').classList.remove('active');
        document.dispatchEvent(new CustomEvent('von:tab-activated', {
            detail: { tabId: 'chatTab' },
        }));
        await jest.advanceTimersByTimeAsync(15000);

        expect(queueReads).toBe(2);
        expect(getJson.mock.calls.some(([url]) => (
            String(url).includes('/claim')
            || String(url).includes('/finish')
            || String(url).includes('/requeue')
        ))).toBe(false);
    });

    test('discards the previous actor response and stops polling after logout', async () => {
        const { getJson } = require(apiServiceModulePath);
        let resolveOldActorQueue;
        let queueReads = 0;
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve({ task_types: [], task_sources: [], defaults: {} });
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({ organisations: [] });
            }
            if (url === '/von/api/chat_prompt_queue') {
                queueReads += 1;
                return new Promise((resolve) => {
                    resolveOldActorQueue = resolve;
                });
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({ tasks: [], count: 0, offset: 0, has_more: false });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        const opening = showGlobalTasks();
        for (let attempt = 0; attempt < 10 && !resolveOldActorQueue; attempt += 1) {
            await Promise.resolve();
        }
        expect(queueReads).toBe(1);

        document.dispatchEvent(new CustomEvent('authStatusChanged', {
            detail: { authenticated: false },
        }));
        resolveOldActorQueue(queueResponse('queued', '2026-09-01T12:00:00Z'));
        await opening;

        expect(document.querySelector('#globalTaskQueueActivity').textContent)
            .toContain('No active or recently failed Von work');
        await jest.advanceTimersByTimeAsync(15000);
        expect(queueReads).toBe(1);
    });
});
