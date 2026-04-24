/** @jest-environment jsdom */

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const apiServiceModulePath = '../../src/frontend/web/von_interface/static/js/apiService.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#test_user', org_id: '#V#test_org' })),
    patchJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('task panel concept links', () => {
    function buildTaxonomyResponse() {
        return {
            task_types: [
                { concept_id: '#V#one_off_task_specification', label: 'One-off' },
            ],
            task_sources: [
                { concept_id: '#V#von_native_task_source', label: 'Von native' },
            ],
            defaults: {
                task_type_id: '#V#one_off_task_specification',
                task_source_id: '#V#von_native_task_source',
            },
        };
    }

    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = `
            <div id="globalTasksContainer"></div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('renders task title and parent as clickable concept links and dispatches concept navigation', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=500')) {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: '#V#task_1282',
                            title: 'Task card concept links',
                            description: 'Make task card links clickable.',
                            status: 'pending',
                            priority: 'medium',
                            task_type_ids: ['#V#one_off_task_specification'],
                            task_source_id: '#V#von_native_task_source',
                            parent_task_concept_id: '#V#task_programme',
                        },
                    ],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();

        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener('open-concept-tab', handler);

        const titleButton = document.querySelector('.task-title.task-concept-link');
        const parentButton = document.querySelector('.task-hierarchy-chip.task-concept-link');

        expect(titleButton).toBeTruthy();
        expect(parentButton).toBeTruthy();
        expect(titleButton.getAttribute('title')).toBe('Open task concept');
        expect(parentButton.getAttribute('title')).toBe('Open parent concept');
        expect(titleButton.getAttribute('aria-label')).toContain('Open task concept');
        expect(parentButton.getAttribute('aria-label')).toContain('Open parent concept');

        titleButton.click();
        parentButton.click();
        parentButton.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
        parentButton.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', bubbles: true }));

        expect(seen).toHaveLength(4);
        expect(seen[0]).toMatchObject({
            conceptId: '#V#task_1282',
            conceptName: 'Task card concept links',
            activate: true,
        });
        expect(seen[1]).toMatchObject({
            conceptId: '#V#task_programme',
            activate: true,
        });
        expect(typeof seen[1].conceptName).toBe('string');
        expect(seen[1].conceptName.length).toBeGreaterThan(0);
        expect(seen[2]).toMatchObject({ conceptId: '#V#task_programme', activate: true });
        expect(seen[3]).toMatchObject({ conceptId: '#V#task_programme', activate: true });

        document.removeEventListener('open-concept-tab', handler);
    });

    test('renders non-clickable title and hierarchy text when concept IDs are missing or non-canonical', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=500')) {
                return Promise.resolve({
                    tasks: [
                        {
                            task_concept_id: 'legacy-task-id',
                            title: 'Legacy task',
                            description: '',
                            status: 'pending',
                            priority: 'low',
                            parent_task_concept_id: 'parent-legacy',
                        },
                    ],
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();

        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener('open-concept-tab', handler);

        const titleText = document.querySelector('.task-title');
        const parentChip = document.querySelector('.task-hierarchy-chip');

        expect(titleText).toBeTruthy();
        expect(parentChip).toBeTruthy();
        expect(document.querySelector('.task-title.task-concept-link')).toBeNull();
        expect(document.querySelector('.task-hierarchy-chip.task-concept-link')).toBeNull();

        titleText.click();
        parentChip.click();
        expect(seen).toHaveLength(0);

        document.removeEventListener('open-concept-tab', handler);
    });

    test('inspector loading still works when using the global task controls', async () => {
        const { getJson } = require(apiServiceModulePath);
        const task = {
            task_concept_id: '#V#task_1282',
            title: 'Detail toggle check',
            description: '',
            status: 'pending',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
            parent_task_concept_id: '#V#task_group',
        };
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=500')) {
                return Promise.resolve({ tasks: [task] });
            }
            if (url === '/api/tasks/%23V%23task_1282') {
                return Promise.resolve(task);
            }
            if (url === '/api/tasks/%23V%23task_1282/comments?limit=100') {
                return Promise.resolve({ comments: [] });
            }
            if (url === '/api/tasks/%23V%23task_1282/attachments?limit=100') {
                return Promise.resolve({ attachments: [] });
            }
            if (url === '/api/tasks/%23V%23task_1282/history?limit=200') {
                return Promise.resolve({ history: [] });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();

        const detailButton = document.querySelector('.task-detail-toggle-btn');
        expect(detailButton).toBeTruthy();
        detailButton.click();

        await flushMicrotasks();
        await flushMicrotasks();

        expect(getJson).toHaveBeenCalledWith('/api/tasks/%23V%23task_1282');
        expect(document.querySelector('.task-inspector-card[data-task-id="#V#task_1282"]')).toBeTruthy();
        expect(document.querySelector('#globalTaskInspector')?.textContent || '').toContain('Detail toggle check');
    });
});
