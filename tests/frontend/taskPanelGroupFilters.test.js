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
        jest.spyOn(console, 'debug').mockImplementation(() => {});
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

    test('project changes replace in-flight loads and ignore late project details', async () => {
        const { getJson, patchJson } = require(apiServiceModulePath);
        let finishOldTasks;
        let finishOldProject;
        const oldTasks = new Promise((resolve) => { finishOldTasks = resolve; });
        const oldProject = new Promise((resolve) => { finishOldProject = resolve; });
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') return Promise.resolve(buildTaxonomyResponse());
            if (url === '/api/tasks/projects') return Promise.resolve({ projects: [
                { concept_id: '#V#project_a', key: 'A', name: 'First project' },
                { concept_id: '#V#project_b', key: 'B', name: 'Second project' },
            ] });
            if (url === '/api/tasks/projects/%23V%23project_a') return oldProject;
            if (url === '/api/tasks/projects/%23V%23project_b') return Promise.resolve({ collections: [
                { concept_id: '#V#collection_b', name: 'Second collection' },
            ] });
            if (url.startsWith('/api/tasks/?')) return oldTasks;
            if (url.startsWith('/api/tasks/search?')) {
                const params = new URL(url, 'https://von.test').searchParams;
                expect(params.get('project_concept_id')).toBe('#V#project_b');
                expect(params.get('bulk_visibility')).toBe('include');
                return Promise.resolve({ tasks: [{ task_concept_id: '#V#new_task', title: 'Second project task', status: 'pending' }], has_more: false });
            }
            return Promise.resolve({});
        });
        const { showGlobalTasks, setCurrentSession } = require(taskPanelModulePath);
        const opening = showGlobalTasks();
        await flushRenderQueue();
        function selectProject(value) {
            const select = document.querySelector('#globalTaskProjectFilter');
            select.value = value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }
        selectProject('#V#project_a');
        selectProject('#V#project_b');
        await flushRenderQueue();
        finishOldProject({ collections: [{ concept_id: '#V#collection_a', name: 'Wrong collection' }] });
        finishOldTasks({ tasks: [{ task_concept_id: '#V#old_task', title: 'Wrong project task', status: 'pending' }] });
        await opening;
        await flushRenderQueue();
        expect(document.querySelector('#globalTaskProjectFilter').value).toBe('#V#project_b');
        expect(document.querySelector('#globalTaskCollectionFilter').textContent).toContain('Second collection');
        expect(document.querySelector('#globalTaskCollectionFilter').textContent).not.toContain('Wrong collection');
        expect(Array.from(document.querySelectorAll('.task-item .task-title')).map((el) => el.textContent.trim())).toEqual(['Second project task']);
        await setCurrentSession('background-conversation');
        patchJson.mockResolvedValue({ success: true });
        const status = document.querySelector('.task-status-select');
        status.value = 'in_progress';
        status.dispatchEvent(new Event('change', { bubbles: true }));
        await flushRenderQueue();
        expect(patchJson).toHaveBeenCalledWith('/api/tasks/%23V%23new_task', { status: 'in_progress' });
        expect(getJson.mock.calls.some(([url]) => url.startsWith('/api/tasks/my'))).toBe(false);
        expect(getJson.mock.calls.some(([url]) => url.includes('session_id=background-conversation'))).toBe(false);
        expect(document.querySelector('.task-item .task-title').textContent.trim()).toBe('Second project task');
    });

    test('derives groups from ontology-linked task parents and filters visible tasks', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=50')) {
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
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=50')) {
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
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=50')) {
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

    test('hides bulk task collections by default and reveals them explicitly', async () => {
        const { getJson } = require(apiServiceModulePath);
        const taskUrls = [];
        const bulkSummary = {
            collection_id: '#V#jira_task_migration_bulk_collection',
            label: 'Jira migration backlog',
            kind: 'jira_migration',
            hidden_by_default: true,
            count: 1,
        };
        const nativeTask = {
            task_concept_id: '#V#task_native',
            title: 'Native task',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        };
        const migratedTask = {
            task_concept_id: '#V#task_migrated',
            title: 'Migrated backlog task',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#jira_imported_task_source',
            hidden_by_default_bulk_task_collections: [bulkSummary],
        };

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                taskUrls.push(url);
                if (url.includes('bulk_visibility=include')) {
                    return Promise.resolve({
                        tasks: [nativeTask, migratedTask],
                        hidden_bulk_task_total: 1,
                        hidden_bulk_task_collections: [bulkSummary],
                    });
                }
                return Promise.resolve({
                    tasks: [nativeTask],
                    hidden_bulk_task_total: 1,
                    hidden_bulk_task_collections: [bulkSummary],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        expect(taskUrls[0]).toContain('bulk_visibility=exclude');
        let visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(['Native task']);

        const control = document.querySelector('#globalBulkTaskVisibilityControl');
        expect(control).toBeTruthy();
        expect(control.textContent).toContain('1 hidden-by-default bulk tasks');
        expect(control.textContent).toContain('Jira migration backlog');

        const showButton = Array.from(control.querySelectorAll('button'))
            .find((button) => (button.textContent || '').includes('Show hidden'));
        expect(showButton).toBeTruthy();
        showButton.click();
        await flushRenderQueue();

        expect(taskUrls.some((url) => url.includes('bulk_visibility=include'))).toBe(true);
        visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(expect.arrayContaining([
            'Native task',
            'Migrated backlog task',
        ]));
        expect(document.querySelector('.task-bulk-collection-chip')?.textContent || '')
            .toContain('Jira migration backlog');
    });

    test('renders incremental load progress and backend timing telemetry for global tasks', async () => {
        const { getJson } = require(apiServiceModulePath);
        const visibleTask = {
            task_concept_id: '#V#task_visible',
            title: 'Visible task',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        };

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({
                    tasks: [visibleTask],
                    hidden_bulk_task_total: 0,
                    hidden_bulk_task_collections: [],
                    load_telemetry: {
                        schema_version: 'task_list_load_telemetry.v1',
                        source: 'task_routes.list_tasks_route',
                        total_ms: 123.45,
                        stages: [
                            {
                                stage: 'parse_request',
                                duration_ms: 1.2,
                                since_start_ms: 1.2,
                            },
                        ],
                        service: {
                            source: 'task_management.list_tasks_with_visibility',
                            total_ms: 98.7,
                            stages: [
                                {
                                    stage: 'repository_find_page',
                                    duration_ms: 44.4,
                                    since_start_ms: 70,
                                },
                            ],
                        },
                    },
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const progress = document.querySelector('#globalTaskLoadProgress');
        expect(progress).toBeTruthy();
        expect(progress.classList.contains('complete')).toBe(true);
        expect(progress.textContent).toContain('Loaded 1 tasks');
        expect(progress.textContent).toContain('Route 123.45 ms');
        expect(progress.textContent).toContain('service 98.7 ms');
        expect(progress.textContent).toContain('repository_find_page');
    });

    test('loads a bounded first page and appends the next page without recounting', async () => {
        const { getJson } = require(apiServiceModulePath);
        const taskUrls = [];
        const buildTask = (index) => ({
            task_concept_id: `#V#task_${index}`,
            title: `Task ${index}`,
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        });

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                taskUrls.push(url);
                if (url.includes('offset=50')) {
                    return Promise.resolve({
                        tasks: [buildTask(51)],
                        count: 1,
                        offset: 50,
                        has_more: false,
                        bulk_summary_included: false,
                    });
                }
                return Promise.resolve({
                    tasks: Array.from({ length: 50 }, (_, index) => buildTask(index + 1)),
                    count: 50,
                    offset: 0,
                    has_more: true,
                    bulk_summary_included: true,
                    hidden_bulk_task_total: 0,
                    hidden_bulk_task_collections: [],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks, getTasks } = require(taskPanelModulePath);
        await showGlobalTasks();

        expect(taskUrls[0]).toContain('limit=50');
        expect(taskUrls[0]).toContain('offset=0');
        expect(taskUrls[0]).toContain('include_total=false');
        expect(taskUrls[0]).toContain('include_bulk_summary=true');
        expect(document.querySelector('[data-task-page-action="more"]')).toBeTruthy();

        document.querySelector('[data-task-page-action="more"]').click();
        await flushRenderQueue();

        expect(taskUrls[1]).toContain('offset=50');
        expect(taskUrls[1]).toContain('include_bulk_summary=false');
        expect(getTasks()).toHaveLength(51);
        expect(document.querySelector('[data-task-page-action="more"]')).toBeNull();
    });

    test('allows a failed first-page request to be retried with Refresh', async () => {
        const { getJson } = require(apiServiceModulePath);
        let taskRequestCount = 0;
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                taskRequestCount += 1;
                if (taskRequestCount === 1) {
                    return Promise.reject(new Error('temporary failure'));
                }
                return Promise.resolve({
                    tasks: [{
                        task_concept_id: '#V#task_recovered',
                        title: 'Recovered task',
                        status: 'pending',
                        priority: 'medium',
                    }],
                    count: 1,
                    offset: 0,
                    has_more: false,
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        expect(document.querySelector('#globalTaskLoadProgress').classList).toContain('error');

        document.querySelector('#refreshGlobalTasksBtn').click();
        await flushRenderQueue();

        expect(taskRequestCount).toBe(2);
        expect(document.body.textContent).toContain('Recovered task');
        expect(document.querySelector('#globalTaskLoadProgress').classList).toContain('complete');
    });

    test('reloads global task scope filters through the backend query', async () => {
        const { getJson } = require(apiServiceModulePath);
        const taskUrls = [];
        const visibleTask = {
            task_concept_id: '#V#task_visible',
            title: 'Scoped task',
            description: '',
            status: 'pending',
            priority: 'medium',
            assignee_concept_id: '#V#group_user',
            created_by_concept_id: '#V#group_user',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        };

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                taskUrls.push(url);
                return Promise.resolve({
                    tasks: [visibleTask],
                    hidden_bulk_task_total: 0,
                    hidden_bulk_task_collections: [],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const scopeFilter = document.querySelector('#globalTaskScopeFilter');
        expect(scopeFilter).toBeTruthy();

        scopeFilter.value = 'assigned_to_me';
        scopeFilter.dispatchEvent(new Event('change', { bubbles: true }));
        await flushRenderQueue();

        expect(taskUrls[taskUrls.length - 1]).toContain(
            'assignee_concept_id=%23V%23group_user',
        );

        scopeFilter.value = 'created_by_me';
        scopeFilter.dispatchEvent(new Event('change', { bubbles: true }));
        await flushRenderQueue();

        expect(taskUrls[taskUrls.length - 1]).toContain(
            'created_by_concept_id=%23V%23group_user',
        );
    });

    test('shows unscoped tasks and assigns one to a represented organisation', async () => {
        const { getJson, patchJson } = require(apiServiceModulePath);
        const unscopedTask = {
            task_concept_id: '#V#task_unscoped',
            title: 'Unscoped task',
            description: 'Needs an organisation',
            status: 'pending',
            priority: 'medium',
            organisation_concept_id: null,
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        };

        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({
                    organisations: [
                        { concept_id: '#V#group_org', name: 'Household', role: 'member' },
                        { concept_id: '#V#other_org', name: 'Other lab', role: 'admin' },
                    ],
                });
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({
                    tasks: [unscopedTask],
                    count: 1,
                    offset: 0,
                    has_more: false,
                });
            }
            if (url === '/api/tasks/%23V%23task_unscoped') {
                return Promise.resolve(unscopedTask);
            }
            if (typeof url === 'string' && url.includes('/comments?')) {
                return Promise.resolve({ comments: [] });
            }
            if (typeof url === 'string' && url.includes('/attachments?')) {
                return Promise.resolve({ attachments: [] });
            }
            if (typeof url === 'string' && url.includes('/history?')) {
                return Promise.resolve({ history: [] });
            }
            return Promise.resolve({});
        });
        patchJson.mockResolvedValue({
            task: { ...unscopedTask, organisation_concept_id: '#V#other_org' },
            changed_fields: ['organisation_concept_id'],
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        expect(document.querySelector('.task-organisation-unscoped')?.textContent || '')
            .toContain('Unscoped');

        document.querySelector('.task-detail-toggle-btn').click();
        await flushRenderQueue();

        const organisationSelect = document.querySelector(
            '#globalTaskInspector .task-detail-organisation-input',
        );
        expect(organisationSelect).toBeTruthy();
        expect(Array.from(organisationSelect.options).map((option) => option.value))
            .toEqual(['', '#V#group_org', '#V#other_org']);

        organisationSelect.value = '#V#other_org';
        document.querySelector('#globalTaskInspector .task-save-fields-btn').click();
        await flushRenderQueue();

        expect(patchJson).toHaveBeenCalledWith(
            '/api/tasks/%23V%23task_unscoped',
            expect.objectContaining({ organisation_concept_id: '#V#other_org' }),
        );
    });

    test('clears old cards and ignores an in-flight response after organisation switch', async () => {
        const { getJson } = require(apiServiceModulePath);
        let resolveOldTasks;
        let taskRequestCount = 0;
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({ organisations: [] });
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                taskRequestCount += 1;
                if (taskRequestCount === 1) {
                    return new Promise((resolve) => {
                        resolveOldTasks = resolve;
                    });
                }
                return Promise.resolve({
                    tasks: [{
                        task_concept_id: '#V#task_new_org',
                        title: 'New organisation task',
                        status: 'pending',
                        priority: 'medium',
                        organisation_concept_id: '#V#new_org',
                    }],
                    count: 1,
                    offset: 0,
                    has_more: false,
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks, getTasks } = require(taskPanelModulePath);
        const opening = showGlobalTasks();
        await flushRenderQueue();

        document.dispatchEvent(new CustomEvent('orgSwitched', {
            detail: { organisation_id: '#V#new_org' },
        }));
        await flushRenderQueue();

        resolveOldTasks({
            tasks: [{
                task_concept_id: '#V#task_old_org',
                title: 'Old organisation task',
                status: 'pending',
                priority: 'medium',
                organisation_concept_id: '#V#group_org',
            }],
            count: 1,
            offset: 0,
            has_more: false,
        });
        await opening;
        await flushRenderQueue();

        expect(taskRequestCount).toBe(2);
        expect(getTasks().map((task) => task.title)).toEqual(['New organisation task']);
        expect(document.body.textContent).not.toContain('Old organisation task');
    });

    test('loads side-panel user tasks with hidden bulk collections excluded by default', async () => {
        document.body.innerHTML = `
            <div id="taskPanel" class="hidden">
                <button id="closeTaskPanel"></button>
                <button id="createTaskBtn"></button>
                <select id="taskStatusFilter"></select>
                <select id="taskPriorityFilter"></select>
                <select id="taskViewMode"></select>
                <input id="taskQueryFilter" />
                <button id="refreshTasksBtn"></button>
                <div id="taskBulkTaskVisibilityControl" class="hidden"></div>
                <div id="taskGroupFilterRow" class="hidden"></div>
                <div id="taskList"></div>
            </div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;

        const { getJson } = require(apiServiceModulePath);
        const taskUrls = [];
        const bulkSummary = {
            collection_id: '#V#jira_task_migration_bulk_collection',
            label: 'Jira migration backlog',
            kind: 'jira_migration',
            hidden_by_default: true,
            count: 1,
        };
        const nativeTask = {
            task_concept_id: '#V#task_native',
            title: 'Native task',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        };
        const migratedTask = {
            task_concept_id: '#V#task_migrated',
            title: 'Migrated backlog task',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#jira_imported_task_source',
            hidden_by_default_bulk_task_collections: [bulkSummary],
        };

        getJson.mockImplementation((url) => {
            if (typeof url === 'string' && url.startsWith('/api/tasks/my?')) {
                taskUrls.push(url);
                if (url.includes('bulk_visibility=include')) {
                    return Promise.resolve({
                        tasks: [nativeTask, migratedTask],
                        hidden_bulk_task_total: 1,
                        hidden_bulk_task_collections: [bulkSummary],
                    });
                }
                return Promise.resolve({
                    tasks: [nativeTask],
                    hidden_bulk_task_total: 1,
                    hidden_bulk_task_collections: [bulkSummary],
                });
            }
            return Promise.resolve({});
        });

        const { initializeTaskPanel, showTaskPanel } = require(taskPanelModulePath);
        initializeTaskPanel();
        showTaskPanel();
        await flushRenderQueue();

        expect(taskUrls[0]).toContain('include_created=true');
        expect(taskUrls[0]).toContain('limit=500');
        expect(taskUrls[0]).toContain('bulk_visibility=exclude');
        let visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(['Native task']);

        const control = document.querySelector('#taskBulkTaskVisibilityControl');
        expect(control).toBeTruthy();
        expect(control.textContent).toContain('1 hidden-by-default bulk tasks');
        const showButton = Array.from(control.querySelectorAll('button'))
            .find((button) => (button.textContent || '').includes('Show hidden'));
        expect(showButton).toBeTruthy();
        showButton.click();
        await flushRenderQueue();

        expect(taskUrls.some((url) => url.includes('bulk_visibility=include'))).toBe(true);
        visibleTitles = Array.from(
            document.querySelectorAll('.task-item .task-title'),
        ).map((el) => (el.textContent || '').trim());
        expect(visibleTitles).toEqual(expect.arrayContaining([
            'Native task',
            'Migrated backlog task',
        ]));
    });

    test('opens side panel for an active session without first loading all user tasks', async () => {
        document.body.innerHTML = `
            <div id="taskPanel" class="hidden">
                <button id="closeTaskPanel"></button>
                <button id="createTaskBtn"></button>
                <select id="taskStatusFilter"></select>
                <select id="taskPriorityFilter"></select>
                <select id="taskViewMode"></select>
                <input id="taskQueryFilter" />
                <button id="refreshTasksBtn"></button>
                <div id="taskBulkTaskVisibilityControl" class="hidden"></div>
                <div id="taskGroupFilterRow" class="hidden"></div>
                <div id="taskList"></div>
            </div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;

        const { getJson } = require(apiServiceModulePath);
        const taskUrls = [];
        getJson.mockImplementation((url) => {
            if (typeof url === 'string' && url.startsWith('/api/tasks')) {
                taskUrls.push(url);
                return Promise.resolve({
                    tasks: [],
                    hidden_bulk_task_total: 0,
                    hidden_bulk_task_collections: [],
                });
            }
            return Promise.resolve({});
        });

        const { initializeTaskPanel, toggleTaskPanel } = require(taskPanelModulePath);
        initializeTaskPanel();
        toggleTaskPanel({ sessionId: 'session-123' });
        await flushRenderQueue();

        expect(taskUrls.some((url) => url.startsWith('/api/tasks/my'))).toBe(false);
        expect(taskUrls[0]).toContain('session_id=session-123');
        expect(taskUrls[0]).toContain('limit=500');
        expect(taskUrls[0]).toContain('bulk_visibility=exclude');
    });
});
