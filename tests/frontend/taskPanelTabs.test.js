/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(), patchJson: jest.fn(), getUserContext: jest.fn(() => ({})),
}));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(id => {
        globalThis.document.querySelectorAll('.tab-content, .tab-button').forEach(el => el.classList.remove('active'));
        globalThis.document.getElementById(id)?.classList.add('active');
        globalThis.document.querySelector(`[data-tab="${id}"]`)?.classList.add('active');
    }),
}));
jest.mock('../../src/frontend/web/von_interface/static/js/markdownUtils.js', () => ({
    renderMarkdownViaServer: jest.fn(async text => `<p>${text}</p>`),
}));
const base = '../../src/frontend/web/von_interface/static/js/';
const task = id => ({ task_concept_id: `#V#${id}`, title: `${id} task`, status: 'pending', notes: `${id} notes` });
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
let panel, api;
beforeEach(() => {
    jest.resetModules();
    document.body.innerHTML = `<div id="tabContainer"><button class="tab-button" data-tab="globalTasksTab">Tasks</button></div>
      <div class="tab-content-area"><div id="globalTasksTab" class="tab-content"><div id="globalTasksContainer"></div></div></div>
      <div id="taskPanel" class="hidden"><button id="closeTaskPanel">Close</button><div id="taskList"></div></div>`;
    api = require(base + 'apiService.js');
    api.getJson.mockImplementation(async url => {
        const match = url.match(/^\/api\/tasks\/%23V%23([^/?]+)$/);
        if (match) return task(match[1]);
        if (url.includes('limit=50')) return { tasks: [task('first'), task('second')] };
        return {};
    });
    panel = require(base + 'components/taskPanel.js');
});

test('popup and board actions open independent task tabs; reopening retains drafts and closing activates a neighbour', async () => {
    await panel.openTaskInPanel('#V#first');
    document.querySelector('#taskList .task-open-in-tab-btn').click();
    await settle();
    expect(document.getElementById('taskPanel').classList.contains('hidden')).toBe(true);
    expect(document.querySelector('.task-tab-content.active').textContent).toContain('first task');
    const firstContent = document.querySelector('.task-tab-content');
    firstContent.querySelector('.task-detail-notes-input').value = 'Unsaved first draft';
    await panel.showGlobalTasks();
    document.querySelector('.task-item[data-task-id="#V#second"] .task-open-in-tab-btn').click();
    await settle();
    expect(document.querySelectorAll('.task-tab-content')).toHaveLength(2);
    expect(document.querySelector('.task-tab-content.active').textContent).toContain('second task');
    expect(firstContent.querySelector('.task-detail-notes-input').value).toBe('Unsaved first draft');
    document.querySelector('#taskTabStack .concept-tab-bucket-toggle').click();
    expect(document.getElementById('taskTabStackOverflow').hidden).toBe(false);
    document.querySelector('#taskTabStackOverflow .task-tab-open').click();
    expect(firstContent.classList.contains('active')).toBe(true);
    await panel.openTaskInTab('#V#first');
    expect(document.querySelectorAll('.task-tab-content')).toHaveLength(2);
    expect(firstContent.querySelector('.task-detail-notes-input').value).toBe('Unsaved first draft');
    document.querySelector('.task-tab-button.active .close-tab').click();
    expect(document.querySelector('.task-tab-content.active').textContent).toContain('second task');
    document.querySelector('.task-tab-button.active .close-tab').click();
    expect(document.getElementById('taskTabStack')).toBeNull();
    expect(document.getElementById('globalTasksTab').classList.contains('active')).toBe(true);
});

test('saving from a tab targets its task after the popup has switched to another task', async () => {
    await panel.openTaskInTab('#V#first');
    await panel.openTaskInPanel('#V#second');
    const content = document.querySelector('.task-tab-content');
    content.querySelector('.task-detail-notes-input').value = 'Updated first notes';
    content.querySelector('.task-save-fields-btn').click();
    await settle();
    expect(api.patchJson).toHaveBeenCalledWith('/api/tasks/%23V%23first', expect.objectContaining({ notes: 'Updated first notes' }));
    expect(content.textContent).toContain('first task');
});

test.each(['orgSwitched', 'authStatusChanged'])('%s removes tabs and ignores late task responses', async eventName => {
    let resolveTask;
    api.getJson.mockImplementation(async url => url === '/api/tasks/%23V%23first'
        ? new Promise(resolve => { resolveTask = resolve; }) : {});
    const opening = panel.openTaskInTab('#V#first');
    document.dispatchEvent(new CustomEvent(eventName, { detail: { organisation_id: '#V#new_org', authenticated: false } }));
    resolveTask(task('first'));
    await opening;
    expect(document.querySelector('.task-tab-content')).toBeNull();
    expect(document.getElementById('taskTabStack')).toBeNull();
    expect(panel.getTasks()).toEqual([]);
});

test('a failed task load has a working retry without creating duplicate tabs', async () => {
    api.getJson.mockRejectedValueOnce(new Error('Temporary failure'));
    await panel.openTaskInTab('#V#first');
    expect(document.querySelector('.task-tab-content').textContent).toContain('Failed to load task details');
    document.querySelector('.task-tab-refresh').click();
    await settle();
    expect(document.querySelector('.task-tab-content').textContent).toContain('first task');
    expect(document.querySelectorAll('.task-tab-content')).toHaveLength(1);
});


test('current work product opens inside the task tab', async () => {
    api.getJson.mockImplementation(async url => {
        if (url === '/api/tasks/%23V%23first') return { ...task('first'), current_work_product: { status: 'ready', concept_id: '#V#product' } };
        if (url === '/api/tasks/%23V%23first/work-product') return { status: 'ready', concept_id: '#V#product', content: 'Delivered research summary' };
        return {};
    });
    await panel.openTaskInTab('#V#first');
    document.querySelector('.task-tab-content .task-open-product-btn').click();
    await settle();
    expect(document.querySelector('.task-tab-content').textContent).toContain('Delivered research summary');
});
