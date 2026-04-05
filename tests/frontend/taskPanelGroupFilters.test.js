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
    function buildTaxonomyResponse() {
        return {
            task_types: [
                { concept_id: '#V#one_off_task_specification', label: 'One-off' },
                { concept_id: '#V#delegated_task_specification', label: 'Delegated' },
            ],
            task_sources: [
                { concept_id: '#V#von_native_task_source', label: 'Von native' },
                { concept_id: '#V#jira_imported_task_source', label: 'Jira migrated' },
            ],
            defaults: {
                task_type_id: '#V#one_off_task_specification',
                task_source_id: '#V#von_native_task_source',
            },
        };
    }

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
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (url === '/api/tasks/?limit=500') {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_a',
                            title: 'Task A',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#one_off_task_specification'],
                            task_source_id: '#V#von_native_task_source',
                            parent_task_concept_id: '#V#activity_alpha',
                        },
                        {
                            task_concept_id: '#V#task_b',
                            title: 'Task B',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#delegated_task_specification'],
                            task_source_id: '#V#jira_imported_task_source',
                            parent_task_concept_id: '#V#activity_beta',
                        },
                        {
                            task_concept_id: '#V#task_c',
                            title: 'Task C',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#one_off_task_specification'],
                            task_source_id: '#V#von_native_task_source',
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
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (url === '/api/tasks/?limit=500') {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_a',
                            title: 'Task A',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#one_off_task_specification'],
                            task_source_id: '#V#von_native_task_source',
                            parent_task_concept_id: '#V#activity_alpha',
                        },
                        {
                            task_concept_id: '#V#task_b',
                            title: 'Task B',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#delegated_task_specification'],
                            task_source_id: '#V#jira_imported_task_source',
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

    test('filters visible tasks by Vontology task type and source', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (url === '/api/tasks/?limit=500') {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_native_one_off',
                            title: 'Native one-off',
                            description: '',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#one_off_task_specification'],
                            task_source_id: '#V#von_native_task_source',
                        },
                        {
                            task_concept_id: '#V#task_jira_delegated',
                            title: 'Jira delegated',
                            description: '',
                            status: 'pending',
                            priority: 'high',
                            task_type_ids: ['#V#delegated_task_specification'],
                            task_source_id: '#V#jira_imported_task_source',
                        },
                    ],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const typeFilter = document.querySelector('#globalTaskTypeFilter');
        const sourceFilter = document.querySelector('#globalTaskSourceFilter');
        expect(typeFilter).toBeTruthy();
        expect(sourceFilter).toBeTruthy();

        typeFilter.value = '#V#delegated_task_specification';
        typeFilter.dispatchEvent(new Event('change', { bubbles: true }));
        sourceFilter.value = '#V#jira_imported_task_source';
        sourceFilter.dispatchEvent(new Event('change', { bubbles: true }));
        await flushRenderQueue();

        const visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(['Jira delegated']);
    });
});
