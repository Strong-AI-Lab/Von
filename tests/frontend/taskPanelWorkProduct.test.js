/** @jest-environment jsdom */
const apiPath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const panelPath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const markdownPath = '../../src/frontend/web/von_interface/static/js/markdownUtils.js';
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(), postJson: jest.fn(), patchJson: jest.fn(), deleteJson: jest.fn(),
    getUserContext: jest.fn(() => ({user_id: '#V#alice', org_id: '#V#lab'})),
}));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({activateTab: jest.fn()}));
jest.mock('../../src/frontend/web/von_interface/static/js/chatTab.js', () => ({switchToChatSession: jest.fn()}));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({showToast: jest.fn()}));
jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    renderMarkdownViaServer: jest.fn(async () => '<p>Canonical current brief</p>'),
}));
const flush = async () => { await new Promise(resolve => setTimeout(resolve, 0)); await new Promise(resolve => setTimeout(resolve, 0)); };
const selected = {
    task_concept_id: '#V#task_a', title: 'Maintain brief A', status: 'pending', priority: 'medium',
    assignee_concept_id: '#V#von_system', created_by_concept_id: '#V#alice', organisation_concept_id: '#V#lab',
    originating_conversation_id: '#V#conversation_a', conversation_session_id: 'session-a',
    task_type_ids: [], current_work_product: {status: 'ready', concept_id: '#V#brief_a'},
};
async function setup({product = {status: 'ready', concept_id: '#V#brief_a', content: 'Current A'}, activity = []} = {}) {
    const api = require(apiPath);
    api.getJson.mockImplementation(async url => {
        if (url === '/api/tasks/taxonomy') return {task_types: [], task_sources: [], defaults: {}};
        if (url === '/von/api/chat_prompt_queue') return {items: activity};
        if (url.startsWith('/api/tasks/?')) return {tasks: [selected], count: 1};
        if (url === '/api/tasks/%23V%23task_a') return selected;
        if (url === '/api/tasks/%23V%23task_a/work-product') return product;
        return {};
    });
    await require(panelPath).showGlobalTasks();
    await flush();
    return api;
}
beforeEach(() => {
    jest.resetModules();
    localStorage.clear();
    jest.spyOn(console, 'debug').mockImplementation(() => {});
    document.body.innerHTML = '<div id="globalTasksContainer"></div><span id="taskCountBadge"></span><span id="globalTaskCountBadge"></span>';
});
afterEach(() => jest.restoreAllMocks());

test('opens only the selected task canonical product and renders its current body', async () => {
    const api = await setup();
    document.querySelector('#globalTaskInspector .task-open-product-btn').click();
    await flush();
    expect(api.getJson).toHaveBeenCalledWith('/api/tasks/%23V%23task_a/work-product');
    expect(require(markdownPath).renderMarkdownViaServer).toHaveBeenCalledWith('Current A');
    expect(document.querySelector('.task-current-product-content').textContent).toBe('Canonical current brief');
});

test.each(['ambiguous_content', 'unavailable'])('does not render stale content when a fresh read reports %s', async status => {
    await setup({product: {status}});
    document.querySelector('#globalTaskInspector .task-open-product-btn').click();
    await flush();
    expect(require(markdownPath).renderMarkdownViaServer).not.toHaveBeenCalled();
    expect(document.querySelector('.task-current-product-content')).toBeNull();
    expect(document.querySelector('.task-open-product-btn')).toBeNull();
});

test('sends a short continuation for the selected task; double click queues once', async () => {
    const api = await setup();
    let finish;
    api.postJson.mockImplementation(() => new Promise(resolve => {finish = resolve;}));
    const input = document.querySelector('#globalTaskInspector .task-continuation-instruction');
    input.value = 'Reconcile the missing observation.';
    input.dispatchEvent(new Event('input'));
    const button = document.querySelector('#globalTaskInspector [data-continue="true"]');
    button.click(); button.click();
    await flush();
    expect(api.postJson).toHaveBeenCalledTimes(1);
    expect(api.postJson.mock.calls[0][0]).toBe('/api/tasks/%23V%23task_a/execute-with-von');
    expect(api.postJson.mock.calls[0][1].continuation_instruction).toBe(input.value);
    finish({success: true, task: selected, queue_item: {status: 'queued'}, task_execution: {status: 'pending'}});
    await flush();
    expect(require('../../src/frontend/web/von_interface/static/js/chatTab.js').switchToChatSession).toHaveBeenCalledWith('session-a');
});

test('an active task is observed without a second launch after loading Tasks', async () => {
    const api = await setup({activity: [{task_concept_id: '#V#task_a', session_id: 'session-a', status: 'in_progress', queue_id: 'existing'}]});
    const button = document.querySelector('#globalTaskInspector [data-continue="true"]');
    expect(button.textContent).toBe('Observe Von work');
    button.click(); await flush();
    expect(api.postJson).not.toHaveBeenCalled();
    expect(require('../../src/frontend/web/von_interface/static/js/chatTab.js').switchToChatSession).toHaveBeenCalledWith('session-a');
});
