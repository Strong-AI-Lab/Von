/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/components/taskPanel.js', () => ({ openTaskInPanel: jest.fn() }));
const response = data => ({ ok: true, json: async () => data });
function setup(taskProvider) {
    document.body.innerHTML = '<input id="vontologySearchInput"><div id="vontologySearchResults"></div>';
    const module = require('../../src/frontend/web/von_interface/static/js/vontology.js');
    const { elements } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');
    elements.vontologySearchInput = document.querySelector('input');
    elements.vontologySearchResults = document.querySelector('div');
    global.fetch = jest.fn(url => {
        if (String(url).includes('/api/tasks/search')) return taskProvider(new URL(String(url), 'http://localhost'));
        if (String(url).includes('/vontology/search')) return Promise.resolve(response({ results: [{ id: '#V#travel', name: 'Travel', kind: 'type' }] }));
        if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(response({ conversations: [] }));
        return Promise.resolve(response({ results: [{ session_id: 'chat1', display_name: 'Travel planning' }] }));
    });
    return module;
}
const task = { task_concept_id: '#V#travel_task', title: 'Book transport', description: 'Arrange conference shuttle', status: 'in_progress', priority: 'medium' };
beforeEach(() => { jest.resetModules(); });
test('searches task content with session-scoped lexical API and opens task details', async () => {
    sessionStorage.setItem('von_window_session_id', 'task-search-window');
    const module = setup(url => {
        expect(url.searchParams.get('query')).toBe('conference shuttle');
        expect(url.searchParams.get('search_mode')).toBe('lexical');
        expect(url.searchParams.has('assignee_concept_id')).toBe(false);
        return Promise.resolve(response({ tasks: [task], total: 1 }));
    });
    await module.performVontologySearch('conference shuttle');
    const row = document.querySelector('.unified-search-task-item');
    expect(row.textContent).toContain('Book transport');
    expect(row.textContent).toContain('conference shuttle');
    expect(row.textContent).toContain('in progress');
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(3);
    const request = global.fetch.mock.calls.find(([url]) => String(url).includes('/api/tasks/search'));
    expect(request[1].headers['X-Von-Window-Session']).toBe('task-search-window');
    row.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(require('../../src/frontend/web/von_interface/static/js/components/taskPanel.js').openTaskInPanel).toHaveBeenCalledWith(task.task_concept_id);
    expect(document.querySelector('#vontologySearchResults').classList.contains('open')).toBe(false);
});
test('task failure preserves conversation and concept results and shows an honest error', async () => {
    const module = setup(() => Promise.resolve({ ok: false, status: 503 }));
    await module.performVontologySearch('transport');
    expect(document.body.textContent).toContain('Task search is temporarily unavailable');
    expect(document.body.textContent).toContain('Travel planning');
    expect(document.body.textContent).toContain('Travel');
});
test('more tasks appends results; untrusted content stays plain text', async () => {
    const module = setup(url => Promise.resolve(response({ tasks: [{ ...task, task_concept_id: `#V#task${url.searchParams.get('offset')}`, title: '<img src=x onerror=alert(1)>' }], total: 2 })));
    await module.performVontologySearch('transport');
    const more = [...document.querySelectorAll('button')].find(button => button.textContent === 'More tasks');
    more.click();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(document.querySelectorAll('.unified-search-task-item')).toHaveLength(2);
    expect(document.querySelector('img')).toBeNull();
    expect(document.body.textContent).not.toContain('More tasks');
});
test('late results cannot replace a newer search and empty tasks are explicit', async () => {
    let resolveOld;
    const module = setup(url => url.searchParams.get('query') === 'old' ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve(response({ tasks: [], total: 0 })));
    const old = module.performVontologySearch('old');
    await module.performVontologySearch('new');
    resolveOld(response({ tasks: [task], total: 1 }));
    await old;
    expect(document.body.textContent).toContain('No matching tasks.');
    expect(document.body.textContent).not.toContain('Book transport');
});
test('keyboard navigation selects a task alongside the existing result kinds', async () => {
    const module = setup(() => Promise.resolve(response({ tasks: [task], total: 1 })));
    module.setupVontologySearchUI();
    await module.performVontologySearch('transport');
    const input = document.querySelector('input');
    const items = module.__test_getUnifiedSearchState().items;
    const index = items.findIndex(item => item.searchType === 'task');
    expect(index).toBeGreaterThanOrEqual(0);
    for (let i = 0; i <= index; i++) input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(require('../../src/frontend/web/von_interface/static/js/components/taskPanel.js').openTaskInPanel).toHaveBeenCalledWith(task.task_concept_id);
});
