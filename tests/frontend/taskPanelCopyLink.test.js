/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(), getUserContext: jest.fn(() => ({})),
    postJson: jest.fn(), patchJson: jest.fn(), deleteJson: jest.fn(),
}));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({ activateTab: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({ showToast: jest.fn() }));
const base = '../../src/frontend/web/von_interface/static/js/';
const taskId = '#V#task_agent_8f35430789702d95864e5def732c43cd';
const task = { task_concept_id: taskId, title: 'Represent deployed Von builds', status: 'completed' };
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
beforeEach(() => {
    jest.resetModules();
    window.history.replaceState({}, '', '/von/');
    document.body.innerHTML = '<div id="taskPanel" class="hidden"><div id="taskList"></div></div><div id="globalTasksContainer"></div>';
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: jest.fn().mockResolvedValue() } });
    document.execCommand = jest.fn(() => false);
    require(base + 'apiService.js').getJson.mockImplementation(async url =>
        url === `/api/tasks/${encodeURIComponent(taskId)}` ? task : {});
});
afterEach(() => jest.restoreAllMocks());

test.each(['popup', 'page'])('%s copies the exact task URL with feedback and no task writes', async surface => {
    const panel = require(base + 'components/taskPanel.js');
    await panel.openTaskInPanel(taskId);
    if (surface === 'page') {
        document.querySelector('.task-open-in-tasks-btn').click();
        await panel.showGlobalTasks();
    }
    const root = document.querySelector(surface === 'popup' ? '#taskPanel' : '#globalTaskInspector');
    const button = root.querySelector('[aria-label="Copy link"]');
    expect(button.classList.contains('copy-id-button')).toBe(true);
    button.focus();
    button.click();
    await settle();
    const copied = navigator.clipboard.writeText.mock.calls[0][0];
    expect(copied).toBe(`http://localhost/von/?task=${encodeURIComponent(taskId)}#globalTasksTab`);
    expect(root.querySelector('[role="status"]').textContent).toBe('Link copied to clipboard');
    expect(document.activeElement).toBe(button);
    if (surface === 'popup') expect(root.classList.contains('hidden')).toBe(false);
    else expect(root.querySelector('.task-inspector-card').dataset.taskId).toBe(taskId);
    const api = require(base + 'apiService.js');
    for (const method of ['postJson', 'patchJson', 'deleteJson']) expect(api[method]).not.toHaveBeenCalled();
});

test('failed clipboard and failed legacy copy report failure; a later retry succeeds', async () => {
    await require(base + 'components/taskPanel.js').openTaskInPanel(taskId);
    navigator.clipboard.writeText.mockRejectedValueOnce(new Error('Denied'));
    const button = document.querySelector('.task-copy-link-btn');
    button.click();
    await settle();
    expect(button.title).toBe('Copy failed');
    expect(document.querySelector('[role="status"]').textContent).toBe('Copy failed');
    expect(document.querySelector('body > textarea')).toBeNull();
    button.click();
    await settle();
    expect(button.title).toBe('Copied to clipboard');
});

test('copied link resolves the exact task beyond the initial list through the task API', async () => {
    const { buildTaskLink } = require(base + 'utils/taskLinks.js');
    window.history.replaceState({}, '', buildTaskLink(taskId));
    await require(base + 'components/taskPanel.js').showGlobalTasks();
    expect(document.querySelector('#globalTaskInspector .task-inspector-card').dataset.taskId).toBe(taskId);
    expect(document.querySelector('.task-item.is-selected').dataset.taskId).toBe(taskId);
});

test('an inaccessible link reports failure without substituting an unrelated task', async () => {
    window.history.replaceState({}, '', '/von/?task=%23V%23inaccessible#globalTasksTab');
    require(base + 'apiService.js').getJson.mockImplementation(async url => {
        if (url === '/api/tasks/%23V%23inaccessible') throw new Error('Forbidden');
        return {};
    });
    await require(base + 'components/taskPanel.js').showGlobalTasks();
    expect(require(base + 'utils/toast.js').showToast).toHaveBeenCalledWith('Could not open linked task', 'error');
    expect(document.querySelector('.task-inspector-card')).toBeNull();
});
