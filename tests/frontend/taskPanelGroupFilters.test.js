/** @jest-environment jsdom */

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const apiServiceModulePath = '../../src/frontend/web/von_interface/static/js/apiService.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#group_user', org_id: '#V#group_org' })),
    patchJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

async function flushRenderQueue() {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('task panel ontology-backed groups', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        document.body.innerHTML = `
            <div id="globalTasksContainer"></div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('derives groups from ontology-linked task parents and filters visible tasks', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/my?include_created=true') {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_a',
                            title: 'Task A',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            parent_task_concept_id: '#V#activity_alpha',
                        },
                        {
                            task_concept_id: '#V#task_b',
                            title: 'Task B',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            parent_task_concept_id: '#V#activity_beta',
                        },
                        {
                            task_concept_id: '#V#task_c',
                            title: 'Task C',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                        },
                    ],
                });
            }
            if (typeof url === 'string' && url.includes('identifier=%23V%23activity_alpha')) {
                return Promise.resolve({
                    raw_doc: {
                        names: [{ name: 'Activity Alpha', language: 'en-NZ', type: 'NL' }],
                    },
                });
            }
            if (typeof url === 'string' && url.includes('identifier=%23V%23activity_beta')) {
                return Promise.resolve({
                    raw_doc: {
                        names: [{ name: 'Activity Beta', language: 'en-NZ', type: 'NL' }],
                    },
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const groupRow = document.querySelector('#globalTaskGroupFilterRow');
        expect(groupRow).toBeTruthy();
        expect(groupRow.classList.contains('hidden')).toBe(false);
        expect(groupRow.textContent).toContain('Activity Alpha');
        expect(groupRow.textContent).toContain('Activity Beta');

        const groupAlphaButton = groupRow.querySelector('[data-group-id="#V#activity_alpha"]');
        expect(groupAlphaButton).toBeTruthy();
        groupAlphaButton.click();
        await flushRenderQueue();

        const visibleTitlesAfterFilter = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitlesAfterFilter).toEqual(['Task A']);

        const allGroupsButton = groupRow.querySelector('[data-group-id=""]');
        expect(allGroupsButton).toBeTruthy();
        allGroupsButton.click();
        await flushRenderQueue();

        const visibleTitlesAfterReset = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitlesAfterReset).toEqual(expect.arrayContaining(['Task A', 'Task B', 'Task C']));
    });

    test('restores persisted group selection between sessions', async () => {
        const { getJson } = require(apiServiceModulePath);
        localStorage.setItem(
            'von_task_group_filter_v1:#V#group_user:#V#group_org',
            JSON.stringify(['#V#activity_beta']),
        );

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/my?include_created=true') {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_a',
                            title: 'Task A',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            parent_task_concept_id: '#V#activity_alpha',
                        },
                        {
                            task_concept_id: '#V#task_b',
                            title: 'Task B',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            parent_task_concept_id: '#V#activity_beta',
                        },
                    ],
                });
            }
            if (typeof url === 'string' && url.includes('identifier=%23V%23activity_alpha')) {
                return Promise.resolve({
                    raw_doc: {
                        names: [{ name: 'Activity Alpha', language: 'en-NZ', type: 'NL' }],
                    },
                });
            }
            if (typeof url === 'string' && url.includes('identifier=%23V%23activity_beta')) {
                return Promise.resolve({
                    raw_doc: {
                        names: [{ name: 'Activity Beta', language: 'en-NZ', type: 'NL' }],
                    },
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(['Task B']);

        const selectedButton = document.querySelector(
            '#globalTaskGroupFilterRow .task-group-filter-btn.active[data-group-id="#V#activity_beta"]',
        );
        expect(selectedButton).toBeTruthy();
    });
});
