/** @jest-environment jsdom */

const fs = require('fs');
const path = require('path');

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const apiServiceModulePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const styles = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css'),
    'utf8',
);

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#layout_user', org_id: '#V#layout_org' })),
    patchJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

function cssRule(selector) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return styles.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`, 'm'))?.[1] || '';
}

function buildTaxonomyResponse() {
    return {
        task_types: [{ concept_id: '#V#one_off_task_specification', label: 'One-off' }],
        task_sources: [{ concept_id: '#V#von_native_task_source', label: 'Von native' }],
        defaults: {
            task_type_id: '#V#one_off_task_specification',
            task_source_id: '#V#von_native_task_source',
        },
    };
}

function buildTasks() {
    return [
        {
            task_concept_id: '#V#task_layout_pending',
            title: 'Pending layout check',
            description: 'A task with ordinary content.',
            status: 'pending',
            assignee_concept_id: '#V#codex_dgx',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        },
        {
            task_concept_id: '#V#task_layout_cancelled_with_a_deliberately_long_identifier',
            title: 'Cancelled layout check with a deliberately long title',
            description: 'A deliberately long_unbroken_value_that_must_not_expand_the_task_board_or_inspector.',
            status: 'cancelled',
            priority: 'medium',
            task_type_ids: ['#V#one_off_task_specification'],
            task_source_id: '#V#von_native_task_source',
        },
    ];
}

async function flushRenderQueue() {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('global task board layout', () => {
    beforeEach(() => {
        jest.resetModules();
        jest.spyOn(console, 'debug').mockImplementation(() => {});
        localStorage.clear();
        document.body.innerHTML = `
            <div id="globalTasksContainer"></div>
            <span id="taskCountBadge" class="hidden"></span>
            <span id="globalTaskCountBadge" class="hidden"></span>
        `;

        const tasks = buildTasks();
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve(buildTaxonomyResponse());
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?') && url.includes('limit=50')) {
                return Promise.resolve({ tasks });
            }
            const detailTask = tasks.find((task) => (
                url === `/api/tasks/${encodeURIComponent(task.task_concept_id)}`
            ));
            if (detailTask) return Promise.resolve(detailTask);
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
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('mounts a labelled board viewport and preserves its scroll position during selection', async () => {
        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const taskList = document.querySelector('#globalTaskList');
        const scrollHint = document.querySelector('#globalTaskBoardScrollHint');
        expect(taskList.dataset.viewMode).toBe('board');
        expect(taskList.getAttribute('role')).toBe('region');
        expect(taskList.getAttribute('tabindex')).toBe('0');
        expect(taskList.getAttribute('aria-label')).toBe('Task board');
        expect(taskList.getAttribute('aria-describedby')).toBe('globalTaskBoardScrollHint');
        expect(scrollHint.classList.contains('hidden')).toBe(false);
        expect(document.querySelectorAll('.task-board-view .task-group')).toHaveLength(6);

        taskList.scrollLeft = 320;
        taskList.scrollTop = 180;
        document.querySelector('[data-task-id="#V#task_layout_pending"]').click();
        expect(taskList.scrollLeft).toBe(320);
        expect(taskList.scrollTop).toBe(180);
    });

    test('labels delegated work and clears selection with focus restoration, including Escape', async () => {
        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();
        const card = () => document.querySelector('.task-item[data-task-id="#V#task_layout_pending"]');
        expect(card().textContent).toContain('Delegated to Codex DGX');
        expect(document.querySelectorAll('.task-item .task-delegation-chip')).toHaveLength(1);
        card().click();
        await flushRenderQueue();
        expect(document.querySelector('#globalTaskInspector').textContent).toContain('Delegated to Codex DGX');
        document.querySelector('.task-clear-selection-btn').click();
        expect(document.querySelector('#globalTaskInspector').textContent).toContain('Select a task');
        expect(document.querySelector('.task-item.is-selected')).toBeNull();
        expect(document.activeElement).toBe(card());
        const viewMode = document.querySelector('#globalTaskViewMode');
        viewMode.value = 'list';
        viewMode.dispatchEvent(new Event('change', { bubbles: true }));
        expect(document.querySelector('.task-item.is-selected')).toBeNull();
        card().click();
        await flushRenderQueue();
        const close = document.querySelector('.task-clear-selection-btn');
        close.focus();
        close.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        expect(document.querySelector('.task-item.is-selected')).toBeNull();
        expect(document.activeElement).toBe(card());
    });

    test('renders and filters deployed work without counting it as active or overdue', async () => {
        const { getJson } = require(apiServiceModulePath);
        const original = getJson.getMockImplementation();
        getJson.mockImplementation((url) => {
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({ tasks: [{ ...buildTasks()[0], status: 'deployed', due_date: '2020-01-01' }] });
            }
            return original(url);
        });
        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();
        const card = document.querySelector('.task-item');
        expect(card.querySelector('.task-status-badge').textContent).toContain('Deployed');
        expect(card.querySelector('.overdue')).toBeNull();
        expect(document.querySelector('#globalTaskSummary').textContent).toMatch(/0 active/);
        expect(card.querySelector('option[value="deployed"]').selected).toBe(true);
        expect(document.querySelectorAll('option[value="deployed"]').length).toBeGreaterThan(1);
        const filter = document.querySelector('#globalTaskStatusFilter');
        filter.value = 'pending';
        filter.dispatchEvent(new Event('change', { bubbles: true }));
        expect(document.querySelector('.task-item')).toBeNull();
        filter.value = 'deployed';
        filter.dispatchEvent(new Event('change', { bubbles: true }));
        expect(document.querySelectorAll('.task-item')).toHaveLength(1);
    });

    test('removes the nested scrolling affordance in list mode', async () => {
        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const viewMode = document.querySelector('#globalTaskViewMode');
        viewMode.value = 'list';
        viewMode.dispatchEvent(new Event('change', { bubbles: true }));

        const taskList = document.querySelector('#globalTaskList');
        expect(taskList.dataset.viewMode).toBe('list');
        expect(taskList.getAttribute('aria-label')).toBe('Task list');
        expect(taskList.hasAttribute('aria-describedby')).toBe(false);
        expect(document.querySelector('#globalTaskBoardScrollHint').classList.contains('hidden')).toBe(true);
        expect(document.querySelector('.task-list-mode')).toBeTruthy();
    });

    test('keeps overflow on the stable board viewport and contains long task content', () => {
        const boardViewportRule = cssRule('.task-list[data-view-mode="board"]');
        const listViewportRule = cssRule('.task-list[data-view-mode="list"]');
        const mainRule = cssRule('.global-task-main');

        expect(boardViewportRule).toContain('overflow: auto');
        expect(boardViewportRule).toContain('flex: 1 1 0');
        expect(boardViewportRule).toContain('min-height: max(420px, 68dvh)');
        expect(boardViewportRule).toContain('overscroll-behavior: contain');
        expect(listViewportRule).toContain('overflow: visible');
        expect(mainRule).not.toContain('overflow-x: auto');
        expect(cssRule('.task-description')).toContain('overflow-wrap: anywhere');
        expect(cssRule('.task-inspector-value')).toContain('overflow-wrap: anywhere');
        expect(cssRule('.task-inspector-heading')).toContain('min-width: 0');
    });
});
