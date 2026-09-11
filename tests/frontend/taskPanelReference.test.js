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
