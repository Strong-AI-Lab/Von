/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

const { sendMessage, showLlmDebugPopup, __test_only__rehydrateHistory, __testOnly_resetChatTtsState } = require(chatTabModulePath);

function advanceTimers(ms) {
    if (jest.isMockFunction(setTimeout)) {
        jest.advanceTimersByTime(ms);
        return Promise.resolve();
    }
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function buildDisplayElementsContract({
    screenText = null,
    spokenText = null,
    tablePayload = null,
    workflowPayload = null,
    taskViewPayload = null,
    timelinePayload = null
} = {}) {
    const elements = [];
    if (typeof screenText === 'string') {
        elements.push({
            element_id: 'screen_text',
            element_type: 'text_block',
            channel: 'screen',
            order: 10,
            intent: 'primary_response',
            payload: { text: screenText },
            provenance: { source: 'test' }
        });
    }
    if (typeof spokenText === 'string') {
        elements.push({
            element_id: 'spoken_text',
            element_type: 'text_block',
            channel: 'spoken',
            order: 20,
            intent: 'narration',
            payload: { text: spokenText },
            provenance: { source: 'test' }
        });
    }
    if (tablePayload && typeof tablePayload === 'object') {
        elements.push({
            element_id: 'screen_table_1',
            element_type: 'table',
            channel: 'screen',
            order: 16,
            intent: 'structured_tabular_view',
            payload: tablePayload,
            provenance: { source: 'test' }
        });
    }
    if (workflowPayload && typeof workflowPayload === 'object') {
        elements.push({
            element_id: 'screen_workflow_view',
            element_type: 'workflow_view',
            channel: 'screen',
            order: 26,
            intent: 'structured_workflow_view',
            payload: workflowPayload,
            provenance: { source: 'test' }
        });
    }
    if (taskViewPayload && typeof taskViewPayload === 'object') {
        elements.push({
            element_id: 'screen_task_view',
            element_type: 'task_view',
            channel: 'screen',
            order: 31,
            intent: 'structured_task_view',
            payload: taskViewPayload,
            provenance: { source: 'test' }
        });
    }
    if (timelinePayload && typeof timelinePayload === 'object') {
        elements.push({
            element_id: 'screen_timeline_view',
            element_type: 'timeline',
            channel: 'screen',
            order: 36,
            intent: 'structured_timeline_view',
            payload: timelinePayload,
            provenance: { source: 'test' }
        });
    }

    return {
        schema_version: 'turn_display_elements_v1',
        elements,
        reason_codes: [],
        validation: { valid: true, errors: [] }
    };
}

describe('chat speech planning (presenter channels)', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="true">
                <div class="thinking-card">
                    <div class="thinking-card-header" id="loadingIndicator">
                        <span class="thinking-card-phase loading-indicator-text">Thinking...</span>
                        <span class="thinking-card-meta" id="thinkingCardMeta"></span>
                        <button id="abortButton" aria-hidden="true"></button>
                    </div>
                    <div class="thinking-card-body" id="loadingIndicatorDetail"></div>
                </div>
            </div>
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
            <input type="checkbox" id="ttsToggle" />

            <div id="chatLlmDebugPopup" class="hidden" aria-hidden="true" data-current-debug-data=""></div>
            <button id="closeChatLlmDebug"></button>
            <button id="copyChatLlmDebugJson"></button>
            <div id="chatLlmDebugMeta"></div>
            <pre id="chatLlmDebugMessages"></pre>
            <pre id="chatLlmDebugResponse"></pre>
            <div id="chatLlmDebugToolsSection" class="hidden"></div>
            <pre id="chatLlmDebugTools"></pre>
            <div id="chatLlmDebugAuxSection" class="hidden"></div>
            <pre id="chatLlmDebugAux"></pre>
        `;

        // Enable TTS in the environment.
        global.SpeechSynthesisUtterance = function (text) {
            this.text = text;
            this.lang = '';
            this.rate = 1;
            this.pitch = 1;
            this.volume = 1;
        };

        global.speechSynthesis = {
            speak: jest.fn(),
            cancel: jest.fn(),
            getVoices: jest.fn(() => [])
        };

        // Ensure module-level TTS state does not leak between tests.
        __testOnly_resetChatTtsState();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
        delete global.SpeechSynthesisUtterance;
        delete global.speechSynthesis;
    });

    test('Speak uses spoken channel and debug popup includes speech_planning', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'SCREEN TEXT',
                        presenter_channels: {
                            screen: 'SCREEN TEXT',
                            spoken: 'SPOKEN TEXT',
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();

        // speech.js intentionally delays speak() briefly after cancel() to avoid
        // first-words clipping in some browsers.
        await new Promise((resolve) => setTimeout(resolve, 150));

        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN TEXT');

        const messageContainers = Array.from(document.querySelectorAll('.message-container'));
        const assistantContainer = messageContainers.find((el) => {
            const turnId = el?.dataset?.turnId;
            return typeof turnId === 'string' && turnId.startsWith('a-');
        });
        const assistantTurnId = assistantContainer?.dataset?.turnId;
        expect(assistantTurnId).toBeTruthy();

        showLlmDebugPopup(assistantTurnId);
        const popup = document.getElementById('chatLlmDebugPopup');
        const jsonText = popup.dataset.currentDebugData;
        expect(jsonText).toContain('speech_planning');
        expect(jsonText).toContain('tts_source');
        expect(jsonText).toContain('spoken');
    });

    test('Speak uses display element contract when presenter channels are missing', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        const displayElements = buildDisplayElementsContract({
            screenText: 'SCREEN FROM CONTRACT',
            spokenText: 'SPOKEN FROM CONTRACT'
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'fallback response',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'fallback response',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 150));

        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN FROM CONTRACT');
    });

    test('renders table display elements from contract payload', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show me task table';

        const displayElements = buildDisplayElementsContract({
            screenText: 'Task summary',
            spokenText: 'Here is the task summary table.',
            tablePayload: {
                columns: [
                    { column_id: 'task', label: 'Task', data_type: 'text', position: 0 },
                    { column_id: 'status', label: 'Status', data_type: 'text', position: 1 }
                ],
                rows: [
                    {
                        row_id: 'row_1',
                        cells: [
                            { column_id: 'task', value_raw: 'Alpha', value_display: 'Alpha', value_type: 'text' },
                            { column_id: 'status', value_raw: 'done', value_display: 'done', value_type: 'text' }
                        ]
                    },
                    {
                        row_id: 'row_2',
                        cells: [
                            { column_id: 'task', value_raw: 'Beta', value_display: 'Beta', value_type: 'text' },
                            { column_id: 'status', value_raw: 'in_progress', value_display: 'in_progress', value_type: 'text' }
                        ]
                    }
                ],
                pagination: { enabled: true, page_size: 50, total_rows: 2 }
            }
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Task summary',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'Task summary',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const renderedTable = document.querySelector('.chat-display-elements-table');
        expect(renderedTable).toBeTruthy();

        const headers = Array.from(renderedTable.querySelectorAll('thead th')).map((cell) => cell.textContent);
        expect(headers).toEqual(['Task', 'Status']);

        const rows = renderedTable.querySelectorAll('tbody tr');
        expect(rows.length).toBe(2);

        const firstRowCells = rows[0].querySelectorAll('td');
        expect(firstRowCells[0].textContent).toBe('Alpha');
        expect(firstRowCells[1].textContent).toBe('done');
    });

    test('applies configured table sort and pagination metadata', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show sorted task table';

        const displayElements = buildDisplayElementsContract({
            screenText: 'Sorted task summary',
            spokenText: 'Here is the sorted task summary table.',
            tablePayload: {
                columns: [
                    { column_id: 'task', label: 'Task', data_type: 'text', position: 0 },
                    { column_id: 'priority', label: 'Priority', data_type: 'number', position: 1 }
                ],
                rows: [
                    {
                        row_id: 'row_1',
                        cells: [
                            { column_id: 'task', value_raw: 'Alpha', value_display: 'Alpha', value_type: 'text' },
                            { column_id: 'priority', value_raw: 1, value_display: '1', value_type: 'number' }
                        ]
                    },
                    {
                        row_id: 'row_2',
                        cells: [
                            { column_id: 'task', value_raw: 'Beta', value_display: 'Beta', value_type: 'text' },
                            { column_id: 'priority', value_raw: 2, value_display: '2', value_type: 'number' }
                        ]
                    },
                    {
                        row_id: 'row_3',
                        cells: [
                            { column_id: 'task', value_raw: 'Gamma', value_display: 'Gamma', value_type: 'text' },
                            { column_id: 'priority', value_raw: 3, value_display: '3', value_type: 'number' }
                        ]
                    }
                ],
                sort: { default_column_id: 'priority', direction: 'desc' },
                pagination: { enabled: true, page_size: 2, total_rows: 3 }
            }
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Sorted task summary',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'Sorted task summary',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const renderedTable = document.querySelector('.chat-display-elements-table');
        expect(renderedTable).toBeTruthy();

        const firstPageRows = renderedTable.querySelectorAll('tbody tr');
        expect(firstPageRows.length).toBe(2);
        expect(firstPageRows[0].querySelectorAll('td')[0].textContent).toBe('Gamma');
        expect(firstPageRows[1].querySelectorAll('td')[0].textContent).toBe('Beta');

        const paginationStatus = document.querySelector('.chat-display-elements-page-status');
        expect(paginationStatus).toBeTruthy();
        expect(paginationStatus.textContent).toBe('Page 1 of 2');

        const nextButton = document.querySelector('.chat-display-elements-page-next');
        expect(nextButton).toBeTruthy();
        nextButton.click();

        const secondPageRows = renderedTable.querySelectorAll('tbody tr');
        expect(secondPageRows.length).toBe(1);
        expect(secondPageRows[0].querySelectorAll('td')[0].textContent).toBe('Alpha');
        expect(paginationStatus.textContent).toBe('Page 2 of 2');
    });

    test('renders workflow view display elements with Von/Jira task links', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show workflow status';

        const displayElements = buildDisplayElementsContract({
            screenText: 'Workflow summary',
            spokenText: 'Here is the workflow summary.',
            workflowPayload: {
                layout: 'list',
                nodes: [
                    {
                        node_id: 'inst_1',
                        label: '#V#salient_predicate_governance_workflow',
                        status: 'running',
                        state: '#V#salience_step_identify_type',
                        progress_label: 'Step 1/3 · Processing',
                        task_links: [
                            {
                                link_type: 'von_task',
                                target_id: '#V#task_alpha',
                                label: '#V#task_alpha'
                            },
                            {
                                link_type: 'jira_issue',
                                target_id: 'JVNAUTOSCI-1148',
                                label: 'JVNAUTOSCI-1148'
                            }
                        ]
                    }
                ],
                edges: []
            }
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Workflow summary',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'Workflow summary',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const onSelect = jest.fn();
        document.addEventListener('von:selectConceptById', onSelect);

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const workflowNode = document.querySelector('.chat-display-elements-workflow-node');
        expect(workflowNode).toBeTruthy();
        expect(workflowNode.textContent).toContain('#V#salient_predicate_governance_workflow');
        expect(workflowNode.textContent).toContain('running');

        const conceptLink = document.querySelector('.chat-display-elements-concept-link');
        expect(conceptLink).toBeTruthy();
        expect(conceptLink.dataset.conceptId).toBe('#V#task_alpha');

        conceptLink.click();
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onSelect.mock.calls[0][0].detail.conceptId).toBe('#V#task_alpha');

        const jiraLink = document.querySelector('a[href*="JVNAUTOSCI-1148"]');
        expect(jiraLink).toBeTruthy();
        expect(jiraLink.getAttribute('href')).toContain('/browse/JVNAUTOSCI-1148');

        document.removeEventListener('von:selectConceptById', onSelect);
    });

    test('renders timeline display elements with task and Jira links', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show task timeline';

        const displayElements = buildDisplayElementsContract({
            screenText: 'Timeline summary',
            spokenText: 'Here is the task timeline.',
            timelinePayload: {
                items: [
                    {
                        item_id: 'event_alpha',
                        label: 'Task status updated',
                        start_at: '2026-02-17T09:10:00Z',
                        status: 'running',
                        description: 'Changed from pending to in_progress',
                        task_links: [
                            {
                                link_type: 'von_task',
                                target_id: '#V#task_alpha',
                                label: '#V#task_alpha'
                            },
                            {
                                link_type: 'jira_issue',
                                target_id: 'JVNAUTOSCI-1138',
                                label: 'JVNAUTOSCI-1138'
                            }
                        ]
                    }
                ]
            }
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Timeline summary',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'Timeline summary',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const onSelect = jest.fn();
        document.addEventListener('von:selectConceptById', onSelect);

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const timelineItem = document.querySelector('.chat-display-elements-timeline-item');
        expect(timelineItem).toBeTruthy();
        expect(timelineItem.textContent).toContain('Task status updated');
        expect(timelineItem.textContent).toContain('Changed from pending to in_progress');

        const conceptLink = document.querySelector('.chat-display-elements-concept-link');
        expect(conceptLink).toBeTruthy();
        expect(conceptLink.dataset.conceptId).toBe('#V#task_alpha');
        conceptLink.click();
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onSelect.mock.calls[0][0].detail.conceptId).toBe('#V#task_alpha');

        const jiraLink = document.querySelector('a[href*="JVNAUTOSCI-1138"]');
        expect(jiraLink).toBeTruthy();
        expect(jiraLink.getAttribute('href')).toContain('/browse/JVNAUTOSCI-1138');

        document.removeEventListener('von:selectConceptById', onSelect);
    });

    test('renders task view display elements with Von/Jira task links', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show task cards';

        const displayElements = buildDisplayElementsContract({
            screenText: 'Task summary',
            spokenText: 'Here are the task cards.',
            taskViewPayload: {
                tasks: [
                    {
                        task_id: '#V#task_alpha',
                        title: 'Alpha task',
                        status: 'in_progress',
                        priority: 'high',
                        due_date: '2026-02-20',
                        assignee: '#V#user_alpha',
                        description: 'Prepare task-view rendering hardening',
                        task_links: [
                            {
                                link_type: 'von_task',
                                target_id: '#V#task_alpha',
                                label: '#V#task_alpha'
                            },
                            {
                                link_type: 'jira_issue',
                                target_id: 'JVNAUTOSCI-1174',
                                href: 'https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1174',
                                label: 'JVNAUTOSCI-1174'
                            }
                        ]
                    }
                ]
            }
        });

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Task summary',
                        display_elements: displayElements,
                        llm_debug: {
                            model: 'gpt-5.2',
                            response: 'Task summary',
                            messages: [],
                            display_elements: displayElements
                        }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const onSelect = jest.fn();
        document.addEventListener('von:selectConceptById', onSelect);

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const taskCard = document.querySelector('.chat-display-elements-task-view-item');
        expect(taskCard).toBeTruthy();
        expect(taskCard.textContent).toContain('Alpha task');
        expect(taskCard.textContent).toContain('Priority: high');
        expect(taskCard.textContent).toContain('Assignee: #V#user_alpha');

        const conceptLink = document.querySelector('.chat-display-elements-concept-link');
        expect(conceptLink).toBeTruthy();
        expect(conceptLink.dataset.conceptId).toBe('#V#task_alpha');
        conceptLink.click();
        expect(onSelect).toHaveBeenCalledTimes(1);
        expect(onSelect.mock.calls[0][0].detail.conceptId).toBe('#V#task_alpha');

        const jiraLink = document.querySelector('a[href*="JVNAUTOSCI-1174"]');
        expect(jiraLink).toBeTruthy();
        expect(jiraLink.getAttribute('href')).toContain('/browse/JVNAUTOSCI-1174');

        document.removeEventListener('von:selectConceptById', onSelect);
    });

    test('Shift+click Speak uses screen channel (accessibility)', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Here is **bold**, and `code`.',
                        presenter_channels: {
                            screen: 'Here is **bold**, and `code`.',
                            spoken: 'SPOKEN TEXT',
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        await new Promise((resolve) => setTimeout(resolve, 150));

        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('Here is bold, and code.');
    });

    test('Default Speak derives narration when spoken missing (never raw markdown)', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Here is **bold**, and `code`.',
                        presenter_channels: {
                            screen: 'Here is **bold**, and `code`.',
                            spoken: null,
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance1 = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance1.text).toBe('Here is bold, and code.');

        // Stop the current speech (same turnId), then speak again with Shift held.
        speakButton.click();

        speakButton.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance2 = global.speechSynthesis.speak.mock.calls[1][0];
        expect(utterance2.text).toBe('Here is bold, and code.');
    });

    test('History Speak backfills spoken talk track on-demand', async () => {
        // Arrange: history contains an assistant turn with no presenter channels.
        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField).toBeTruthy();

        global.fetch = jest.fn((url, _init) => {
            if (typeof url === 'string' && url.startsWith('/von/history/backfill_spoken')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'ok',
                        presenter_channels: {
                            screen: 'SCREEN TEXT',
                            spoken: 'SPOKEN FROM BACKFILL',
                            format: 'narration_fallback_v1'
                        },
                        updated: true
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __test_only__rehydrateHistory(scrollableField, [
            { role: 'user', content: 'Hi', timestamp: '2026-01-02T00:00:00Z' },
            {
                role: 'assistant',
                content: 'SCREEN TEXT',
                timestamp: '2026-01-02T00:00:01Z',
                history_location: { session_id: 'sess-1', history_index: 1 }
            }
        ]);

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        // Act
        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 0));

        await new Promise((resolve) => setTimeout(resolve, 200));

        // Assert
        expect(global.fetch).toHaveBeenCalled();
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN FROM BACKFILL');
    });

    test('History Speak uses display elements returned by backfill endpoint', async () => {
        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField).toBeTruthy();

        const displayElements = buildDisplayElementsContract({
            screenText: 'SCREEN TEXT',
            spokenText: 'SPOKEN FROM CONTRACT BACKFILL'
        });

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/history/backfill_spoken')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'ok',
                        display_elements: displayElements,
                        updated: true
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __test_only__rehydrateHistory(scrollableField, [
            { role: 'user', content: 'Hi', timestamp: '2026-01-02T00:00:00Z' },
            {
                role: 'assistant',
                content: 'SCREEN TEXT',
                timestamp: '2026-01-02T00:00:01Z',
                history_location: { session_id: 'sess-1', history_index: 1 }
            }
        ]);

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 200));

        expect(global.fetch).toHaveBeenCalled();
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN FROM CONTRACT BACKFILL');
    });

    test('History Speak falls back to on-screen text when backfill fails (and cools down)', async () => {
        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField).toBeTruthy();

        const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => { });
        let backfillCalls = 0;
        global.fetch = jest.fn((url, _init) => {
            if (typeof url === 'string' && url.startsWith('/von/history/backfill_spoken')) {
                backfillCalls += 1;
                return Promise.resolve({
                    ok: false,
                    status: 404,
                    json: async () => ({ error: 'History entry not found' })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __test_only__rehydrateHistory(scrollableField, [
            { role: 'user', content: 'Hi', timestamp: '2026-01-02T00:00:00Z' },
            {
                role: 'assistant',
                content: 'SCREEN TEXT',
                timestamp: '2026-01-02T00:00:01Z',
                history_location: { session_id: 'sess-1', history_index: 1 }
            }
        ]);

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await advanceTimers(200);
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance1 = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance1.text).toBe('SCREEN TEXT');

        const notice1 = document.querySelector('.chat-tts-notice');
        expect(notice1).toBeTruthy();
        expect(String(notice1.textContent || '')).toContain('Talk track unavailable');

        expect(backfillCalls).toBe(1);
        expect(warnSpy).toHaveBeenCalled();

        // Second click stops the current speech (toggle behaviour).
        speakButton.click();

        // The click handler is async (history backfill/cool-down). Give it a tick to
        // restore the button state before clicking again.
        await new Promise((resolve) => setTimeout(resolve, 0));

        // Third click within cooldown should speak again without retrying backfill.
        speakButton.click();
        await advanceTimers(200);
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance2 = global.speechSynthesis.speak.mock.calls[1][0];
        expect(utterance2.text).toBe('SCREEN TEXT');
        expect(backfillCalls).toBe(1);
    });
});
