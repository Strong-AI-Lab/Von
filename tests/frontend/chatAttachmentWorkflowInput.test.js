/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'window-attachment-test')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    createVontologyCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    getCartoucheAppearanceSettings: jest.fn(() => ({})),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

describe('chat attachment workflow input binding', () => {
    beforeEach(() => {
        window.scrollTo = jest.fn();
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
        `;

        const { getUserContext } = require(
            '../../src/frontend/web/von_interface/static/js/apiService.js'
        );
        getUserContext.mockReturnValue({
            user_id: '#V#attachment_test_user',
            org_id: null,
            language: 'en-NZ',
            gmail_profile: null
        });

        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_resetChatRequestState();
        chatTab.__testOnly_setActiveChatSession('attachment-session', 'Attachment test');
    });

    afterEach(() => {
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_resetChatRequestState();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('normal upload binds its trusted file-copy id to the next generate request', async () => {
        const {
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_test';
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        uploaded: {
                            concept_id: fileCopyConceptId,
                            type_concept_id: '#V#computer_file_copy'
                        },
                        storage: {
                            backend: 'test',
                            key: 'uploads/test/supervision.xlsx',
                            uri: 'test://uploads/test/supervision.xlsx'
                        },
                        chat_history_recorded: true
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Spreadsheet workflow started.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(
            ['student,supervisor\nAlice,Professor Example\n'],
            'supervision.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
        );
        await __testOnly_uploadFilesToVon([uploadedFile]);

        const promptInput = document.getElementById('promptInput');
        expect(promptInput.value).toContain('Attached file uploaded.');
        expect(promptInput.value).not.toContain('supervision.xlsx');
        expect(promptInput.value).not.toContain(fileCopyConceptId);
        promptInput.value += '\nRepresent the PhD students and supervision information.';

        await sendMessage();

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
        expect(generateBodies[0].prompt).not.toContain(fileCopyConceptId);
        expect(generateBodies[0].prompt).not.toContain('supervision.xlsx');

        promptInput.value = 'A separate follow-up without an attachment.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[1]).not.toHaveProperty('workflow_inputs');
    }, 15000);

    test('a rejected generate request retains the attachment for retry', async () => {
        const {
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_retry_test';
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        uploaded: {
                            concept_id: fileCopyConceptId,
                            type_concept_id: '#V#computer_file_copy'
                        },
                        storage: { backend: 'test', key: 'uploads/test/retry.xlsx' },
                        chat_history_recorded: true
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                if (generateBodies.length === 1) {
                    return Promise.resolve({
                        ok: false,
                        status: 503,
                        json: async () => ({ error: 'temporary_unavailable' })
                    });
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Spreadsheet workflow started.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(
            ['candidate,supervisor\nExample,Professor Example\n'],
            'retry.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
        );
        await __testOnly_uploadFilesToVon([uploadedFile]);

        const promptInput = document.getElementById('promptInput');
        promptInput.value += '\nRepresent this spreadsheet.';
        await sendMessage();

        promptInput.value = 'Retry the spreadsheet representation.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[0].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
        expect(generateBodies[1].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);

    test('a session switch cannot consume another session attachment', async () => {
        const {
            __testOnly_setActiveChatSession,
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_session_test';
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        uploaded: {
                            concept_id: fileCopyConceptId,
                            type_concept_id: '#V#computer_file_copy'
                        },
                        storage: { backend: 'test', key: 'uploads/test/session.xlsx' },
                        chat_history_recorded: true
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Request completed.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(
            ['candidate,supervisor\nExample,Professor Example\n'],
            'session.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
        );
        await __testOnly_uploadFilesToVon([uploadedFile]);

        const promptInput = document.getElementById('promptInput');
        __testOnly_setActiveChatSession('unrelated-session', 'Unrelated');
        promptInput.value = 'An unrelated question.';
        await sendMessage();

        __testOnly_setActiveChatSession('attachment-session', 'Attachment test');
        promptInput.value = 'Represent the uploaded spreadsheet.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[0]).not.toHaveProperty('workflow_inputs');
        expect(generateBodies[1].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);

    test('an upload with no active session creates and binds its own conversation', async () => {
        const {
            __testOnly_setActiveChatSession,
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_new_session_test';
        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/session/create_chat_session') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        session_id: 'upload-created-session',
                        session_name: 'Upload conversation',
                        history: []
                    })
                });
            }
            if (url === '/von/api/files/upload') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        uploaded: {
                            concept_id: fileCopyConceptId,
                            type_concept_id: '#V#computer_file_copy'
                        },
                        storage: { backend: 'test', key: 'uploads/test/new-session.xlsx' },
                        chat_history_recorded: true
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Request completed.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __testOnly_setActiveChatSession(null, null);
        const uploadedFile = new File(
            ['candidate,supervisor\nExample,Professor Example\n'],
            'new-session.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
        );
        await __testOnly_uploadFilesToVon([uploadedFile]);

        const promptInput = document.getElementById('promptInput');
        __testOnly_setActiveChatSession('unrelated-session', 'Unrelated');
        promptInput.value = 'An unrelated question.';
        await sendMessage();

        __testOnly_setActiveChatSession(
            'upload-created-session',
            'Upload conversation'
        );
        promptInput.value = 'Represent the uploaded spreadsheet.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[0]).not.toHaveProperty('workflow_inputs');
        expect(generateBodies[1].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);

    test('queued prompts carry only the attachment bound to that prompt', async () => {
        const {
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_queue_test';
        const generateBodies = [];
        let resolveFirstGenerate;
        let queueCounter = 0;
        const queueRecords = new Map();

        const firstGenerateResponse = new Promise((resolve) => {
            resolveFirstGenerate = resolve;
        });

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        uploaded: {
                            concept_id: fileCopyConceptId,
                            type_concept_id: '#V#computer_file_copy'
                        },
                        storage: { backend: 'test', key: 'uploads/test/queue.xlsx' },
                        chat_history_recorded: true
                    })
                });
            }
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueCounter += 1;
                const queueId = `queue-${queueCounter}`;
                queueRecords.set(queueId, {
                    queue_id: queueId,
                    prompt_raw: body.prompt_raw,
                    session_id: body.session_id,
                    session_name: body.session_name,
                    status: body.status || 'queued'
                });
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: queueRecords.get(queueId)
                    })
                });
            }
            if (
                typeof url === 'string'
                && url.startsWith('/von/api/chat_prompt_queue/')
            ) {
                const queueId = url.split('/')[4];
                const body = JSON.parse(options.body || '{}');
                const existing = queueRecords.get(queueId) || {};
                const status = url.endsWith('/claim')
                    ? 'in_progress'
                    : (url.endsWith('/finish') ? body.status : 'queued');
                const updated = {
                    ...existing,
                    ...(body.prompt_raw ? { prompt_raw: body.prompt_raw } : {}),
                    status
                };
                queueRecords.set(queueId, updated);
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: updated
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateBodies.push(JSON.parse(options.body || '{}'));
                if (generateBodies.length === 1) {
                    return firstGenerateResponse;
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Request completed.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Keep the first request busy.';
        const firstSend = sendMessage();
        await Promise.resolve();

        promptInput.value = 'An unrelated queued prompt.';
        await sendMessage();

        const uploadedFile = new File(
            ['candidate,supervisor\nExample,Professor Example\n'],
            'queue.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            }
        );
        await __testOnly_uploadFilesToVon([uploadedFile]);
        promptInput.value += '\nRepresent the uploaded spreadsheet.';
        await sendMessage();

        resolveFirstGenerate({
            ok: true,
            json: async () => ({
                response: 'First request completed.',
                llm_debug: { model: 'test-model' }
            })
        });
        await firstSend;

        for (let attempt = 0; attempt < 40 && generateBodies.length < 3; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 10));
        }

        expect(generateBodies).toHaveLength(3);
        expect(generateBodies[0]).not.toHaveProperty('workflow_inputs');
        expect(generateBodies[1]).not.toHaveProperty('workflow_inputs');
        expect(generateBodies[2].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);
});
