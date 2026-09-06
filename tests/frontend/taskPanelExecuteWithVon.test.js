/** @jest-environment jsdom */

const taskPanelModulePath = '../../src/frontend/web/von_interface/static/js/components/taskPanel.js';
const apiServiceModulePath = '../../src/frontend/web/von_interface/static/js/apiService.js';
const toastModulePath = '../../src/frontend/web/von_interface/static/js/utils/toast.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    deleteJson: jest.fn(),
    getJson: jest.fn(),
    getUserContext: jest.fn(() => ({
        user_id: '#V#alice',
        org_id: '#V#research_lab',
    })),
    patchJson: jest.fn(),
    postJson: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({
    activateTab: jest.fn(),
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn(),
}));

function task(overrides = {}) {
    return {
        task_concept_id: '#V#task_prepare_brief',
        title: 'Prepare brief',
        description: 'Prepare and read back the research brief.',
        status: 'pending',
        priority: 'medium',
        assignee_concept_id: '#V#von_system',
        created_by_concept_id: '#V#alice',
        organisation_concept_id: '#V#research_lab',
        originating_conversation_id: '#V#conversation_1',
        conversation_session_id: 'session-1',
        conversation_name: 'Research planning',
        task_type_ids: [],
        ...overrides,
    };
}

function queueResponse() {
    return {
        success: true,
        items: [{
            queue_id: 'queue-1',
            status: 'queued',
            task_concept_id: '#V#task_prepare_brief',
            task_execution_concept_id: '#V#task_execution_1',
            session_id: 'session-1',
            session_name: 'Research planning',
            updated_at: '2026-09-01T12:00:00Z',
        }],
        recent_failed_items: [],
    };
}

async function flushRenderQueue() {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('explicit Execute with Von task control', () => {
    beforeEach(() => {
        jest.resetModules();
        jest.spyOn(console, 'debug').mockImplementation(() => {});
        jest.spyOn(console, 'error').mockImplementation(() => {});
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

    test('launches from the global inspector, disables in flight, and refreshes observer activity', async () => {
        const { getJson, postJson } = require(apiServiceModulePath);
        const queuedTask = task();
        let resolveLaunch;
        let queueActivityLoads = 0;
        postJson.mockImplementation(() => new Promise((resolve) => {
            resolveLaunch = resolve;
        }));
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve({ task_types: [], task_sources: [], defaults: {} });
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({ organisations: [] });
            }
            if (url === '/von/api/chat_prompt_queue') {
                queueActivityLoads += 1;
                return Promise.resolve(
                    queueActivityLoads === 1
                        ? { success: true, items: [], recent_failed_items: [] }
                        : queueResponse(),
                );
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({
                    tasks: [queuedTask],
                    count: 1,
                    offset: 0,
                    has_more: false,
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        const inspectorButton = document.querySelector(
            '#globalTaskInspector .task-execute-with-von-btn',
        );
        expect(inspectorButton).toBeTruthy();
        expect(inspectorButton.title).toContain('Research planning');
        expect(document.querySelector('#globalTaskQueueActivity').textContent).toContain(
            'No active or recently failed Von work',
        );
        expect(document.querySelector('#globalTaskQueueActivity .task-delete-btn')).toBeNull();

        inspectorButton.click();
        await Promise.resolve();
        expect(inspectorButton.disabled).toBe(true);
        expect(inspectorButton.textContent).toBe('Queuing…');
        expect(postJson).toHaveBeenCalledTimes(1);
        const [url, payload] = postJson.mock.calls[0];
        expect(url).toBe('/api/tasks/%23V%23task_prepare_brief/execute-with-von');
        expect(typeof payload.launch_request_id).toBe('string');
        expect(payload.launch_request_id.length).toBeGreaterThan(8);

        resolveLaunch({
            success: true,
            task: { conversation_name: 'Research planning' },
            task_execution: { status: 'pending' },
            queue_item: { status: 'queued' },
        });
        await flushRenderQueue();

        expect(inspectorButton.disabled).toBe(false);
        expect(inspectorButton.textContent).toBe('Observe Von work');
        inspectorButton.click();
        await flushRenderQueue();
        expect(postJson).toHaveBeenCalledTimes(1);
        expect(require(toastModulePath).showToast).toHaveBeenCalledWith(
            'Von work queued in Research planning',
            'success',
        );
        expect(getJson.mock.calls.filter(([url]) => url === '/von/api/chat_prompt_queue')).toHaveLength(2);
        expect(localStorage.length).toBe(1);
    });

    test.each([
        ['completed', 'Von work already completed in Research planning', 'success'],
        ['failed', 'Von work failed; the task can be retried', 'error'],
        ['cancelled', 'Von work was cancelled; the task can be retried', 'info'],
    ])(
        'reuses a durable launch identity after a lost response and clears a %s replay',
        async (terminalStatus, expectedMessage, expectedToastType) => {
            const { getJson, postJson } = require(apiServiceModulePath);
            getJson.mockImplementation((url) => {
                if (url === '/api/tasks/taxonomy') {
                    return Promise.resolve({ task_types: [], task_sources: [], defaults: {} });
                }
                if (url === '/von/api/organisations/my_organisations') {
                    return Promise.resolve({ organisations: [] });
                }
                if (url === '/von/api/chat_prompt_queue') {
                    return Promise.resolve({ success: true, items: [], recent_failed_items: [] });
                }
                if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                    return Promise.resolve({
                        tasks: [task()], count: 1, offset: 0, has_more: false,
                    });
                }
                return Promise.resolve({});
            });
            postJson
                .mockRejectedValueOnce(new Error('response lost'))
                .mockResolvedValueOnce({
                    success: true,
                    task: { conversation_name: 'Research planning' },
                    task_execution: { status: terminalStatus },
                    queue_item: { status: terminalStatus },
                });

            const { showGlobalTasks } = require(taskPanelModulePath);
            await showGlobalTasks();
            await flushRenderQueue();

            document.querySelector('.task-execute-with-von-btn').click();
            await flushRenderQueue();
            const firstLaunchId = postJson.mock.calls[0][1].launch_request_id;
            expect(localStorage.length).toBe(1);

            document.querySelector('.task-execute-with-von-btn').click();
            await flushRenderQueue();
            expect(postJson.mock.calls[1][1].launch_request_id).toBe(firstLaunchId);
            expect(localStorage.length).toBe(0);
            expect(document.querySelector('.task-execute-with-von-btn').disabled).toBe(false);
            expect(require(toastModulePath).showToast).toHaveBeenLastCalledWith(
                expectedMessage,
                expectedToastType,
            );
            postJson.mockResolvedValueOnce({success: true, task: {conversation_name: 'Research planning'}, queue_item: {status: 'queued'}});
            document.querySelector('.task-execute-with-von-btn').click();
            await flushRenderQueue();
            expect(postJson.mock.calls[2][1].launch_request_id).not.toBe(firstLaunchId);
        },
    );

    test('can create a conversation-associated task assigned to Von', async () => {
        const { getJson, postJson } = require(apiServiceModulePath);
        document.body.innerHTML = `
            <div id="taskPanel" class="hidden">
                <input id="newTaskTitle" />
                <textarea id="newTaskDescription"></textarea>
                <select id="newTaskAssignee">
                    <option value="">Unassigned</option>
                    <option value="#V#von_system">Assign to Von</option>
                </select>
                <select id="newTaskPriority"><option value="medium">Medium</option></select>
                <button id="createTaskBtn">Create Task</button>
                <button id="closeTaskPanel">Close</button>
                <button id="refreshTasksBtn">Refresh</button>
                <select id="taskStatusFilter"><option value="all">All</option></select>
                <select id="taskPriorityFilter"><option value="all">All</option></select>
                <input id="taskQueryFilter" />
                <select id="taskViewMode"><option value="board">Board</option></select>
                <div id="taskList"></div>
            </div>
        `;
        getJson.mockResolvedValue({ tasks: [] });
        postJson.mockResolvedValue({ task_concept_id: '#V#created_task' });

        const { initializeTaskPanel, setCurrentSession } = require(taskPanelModulePath);
        initializeTaskPanel();
        await setCurrentSession('session-1');
        document.getElementById('newTaskTitle').value = 'Prepare brief';
        document.getElementById('newTaskDescription').value = 'Prepare the brief.';
        document.getElementById('newTaskAssignee').value = '#V#von_system';
        document.getElementById('createTaskBtn').click();
        await flushRenderQueue();

        expect(postJson).toHaveBeenCalledWith('/api/tasks/', {
            title: 'Prepare brief',
            description: 'Prepare the brief.',
            priority: 'medium',
            assignee_concept_id: '#V#von_system',
            session_id: 'session-1',
        });
    });

    test('does not offer execution for a task not authorised by this actor', async () => {
        const { getJson } = require(apiServiceModulePath);
        getJson.mockImplementation((url) => {
            if (url === '/api/tasks/taxonomy') {
                return Promise.resolve({ task_types: [], task_sources: [], defaults: {} });
            }
            if (url === '/von/api/organisations/my_organisations') {
                return Promise.resolve({ organisations: [] });
            }
            if (url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({ success: true, items: [], recent_failed_items: [] });
            }
            if (typeof url === 'string' && url.startsWith('/api/tasks/?')) {
                return Promise.resolve({
                    tasks: [task({ created_by_concept_id: '#V#bob' })],
                    count: 1,
                    offset: 0,
                    has_more: false,
                });
            }
            return Promise.resolve({});
        });

        const { showGlobalTasks } = require(taskPanelModulePath);
        await showGlobalTasks();
        await flushRenderQueue();

        expect(document.querySelector('.task-execute-with-von-btn')).toBeNull();
    });
});
