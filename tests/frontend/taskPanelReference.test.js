/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), getUserContext: jest.fn(() => ({})) }));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({ activateTab: jest.fn() }));
const base = '../../src/frontend/web/von_interface/static/js/';
beforeEach(() => jest.resetModules());
test('opens a completed referenced task from Messages outside the hidden Conversations tab', async () => {
    document.body.innerHTML = '<div id="chatTab" hidden><div id="taskPanel" class="hidden"><div id="taskList"></div></div></div><div id="messagesTab"></div>';
    const { getJson } = require(base + 'apiService.js');
    getJson.mockImplementation(async url => url === '/api/tasks/%23V%23exact_task' ? { task_concept_id: '#V#exact_task', title: 'Exact completed task', status: 'completed', description: 'Original instructions' } : {});
    await require(base + 'components/taskPanel.js').openTaskInPanel('#V#exact_task');
    expect(document.getElementById('taskPanel').parentElement).toBe(document.body);
    expect(document.getElementById('taskPanel').classList.contains('hidden')).toBe(false);
    expect(document.getElementById('taskList').textContent).toContain('Exact completed task');
    expect(document.querySelector('.task-detail-panel')).toBeTruthy();
    expect(document.getElementById('taskList').dataset.viewMode).toBe('list');
    expect(getJson.mock.calls.some(([url]) => url.startsWith('/api/tasks/my'))).toBe(false);
});

test('an older task response cannot replace the most recently selected task', async () => {
    document.body.innerHTML = '<div id="taskPanel" class="hidden"><div id="taskList"></div></div>';
    const { getJson } = require(base + 'apiService.js');
    let resolveEarlier;
    getJson.mockImplementation(async url => {
        if (url === '/api/tasks/%23V%23earlier_task') return new Promise(resolve => { resolveEarlier = resolve; });
        if (url === '/api/tasks/%23V%23latest_task') return { task_concept_id: '#V#latest_task', title: 'Latest selected task', status: 'completed' };
        return {};
    });
    const { openTaskInPanel } = require(base + 'components/taskPanel.js');
    const earlier = openTaskInPanel('#V#earlier_task');
    await openTaskInPanel('#V#latest_task');
    resolveEarlier({ task_concept_id: '#V#earlier_task', title: 'Earlier task', status: 'pending' });
    await earlier;
    expect(document.getElementById('taskList').textContent).toContain('Latest selected task');
    expect(document.getElementById('taskList').textContent).not.toContain('Earlier task');
});

test('popup exposes explicit close, keyboard dismissal, outside dismissal and detail state', async () => {
    document.body.innerHTML = '<button id="opener">Task reference</button><button id="outside">Outside</button><div id="taskPanel" class="hidden"><button id="closeTaskPanel">Close</button><div id="taskList"></div></div>';
    const { getJson } = require(base + 'apiService.js');
    getJson.mockImplementation(async url => url === '/api/tasks/%23V%23exact_task' ? { task_concept_id: '#V#exact_task', title: 'Exact task', status: 'completed' } : {});
    const panel = require(base + 'components/taskPanel.js');
    const opener = document.getElementById('opener');
    opener.focus();
    await panel.openTaskInPanel('#V#exact_task');
    expect(document.activeElement.id).toBe('closeTaskPanel');
    const details = document.querySelector('.task-detail-toggle-btn');
    expect(details.textContent.trim()).toBe('Hide details');
    expect(details.getAttribute('aria-expanded')).toBe('true');
    details.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(document.querySelector('.task-detail-toggle-btn').getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement.classList.contains('task-detail-toggle-btn')).toBe(true);
    document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(document.getElementById('taskPanel').getAttribute('aria-hidden')).toBe('true');
    expect(document.activeElement).toBe(opener);
    await panel.openTaskInPanel('#V#exact_task');
    document.getElementById('outside').dispatchEvent(new Event('pointerdown', { bubbles: true }));
    expect(document.getElementById('taskPanel').classList.contains('hidden')).toBe(true);
    await panel.openTaskInPanel('#V#exact_task');
    document.getElementById('closeTaskPanel').click();
    expect(document.getElementById('taskPanel').classList.contains('hidden')).toBe(true);
});

test('Open in Tasks closes the popup and selects the exact task beyond the first page and saved filters', async () => {
    document.body.innerHTML = '<div id="taskPanel" class="hidden"><button id="closeTaskPanel">Close</button><select id="taskStatusFilter"><option value="pending">Pending</option></select><div id="taskList"></div></div><div id="globalTasksContainer"></div>';
    const { getJson } = require(base + 'apiService.js');
    getJson.mockImplementation(async url => {
        if (url === '/api/tasks/%23V%23exact_task') return { task_concept_id: '#V#exact_task', title: 'Exact completed task', status: 'completed' };
        if (url.includes('limit=50')) return { tasks: [{ task_concept_id: '#V#other', title: 'Other task', status: 'pending' }] };
        return {};
    });
    const panel = require(base + 'components/taskPanel.js');
    await panel.openTaskInPanel('#V#exact_task');
    document.getElementById('taskStatusFilter').dispatchEvent(new Event('change'));
    document.querySelector('.task-open-in-tab-btn').click();
    expect(document.getElementById('taskPanel').classList.contains('hidden')).toBe(true);
    expect(require(base + 'tabNavigation.js').activateTab).toHaveBeenCalledWith('globalTasksTab');
    await panel.showGlobalTasks();
    expect(document.querySelector('#globalTaskInspector').textContent).toContain('Exact completed task');
    expect(document.querySelector('.task-item.is-selected').dataset.taskId).toBe('#V#exact_task');
    expect(document.getElementById('globalTaskStatusFilter').value).toBe('pending');
    expect(document.activeElement.id).toBe('globalTaskInspector');
});
