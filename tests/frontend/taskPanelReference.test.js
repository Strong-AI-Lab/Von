/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), getUserContext: jest.fn(() => ({})) }));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({ activateTab: jest.fn() }));
const base = '../../src/frontend/web/von_interface/static/js/';
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
