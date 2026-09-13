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
            <div id="chatAttachmentStatus" class="chat-attachment-status hidden">
                <span id="uploadFileStatus" class="upload-file-status"></span>
                <span id="pendingAttachmentStatus" class="pending-attachment-status hidden"></span>
            </div>
            <button id="uploadFileButton">Upload File</button>
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

    test('phone image without MIME binds only to its next request, after preview and removal', async () => {
        const { __testOnly_uploadFilesToVon, sendMessage } = require(chatTabModulePath);
        const bodies = [];
        let uploaded = 0;
        global.fetch = jest.fn(async (url, options = {}) => {
            if (url === '/von/api/images/upload') return {ok:true, json:async () => ({image_attachment:{concept_id:`#V#phone-${++uploaded}`, filename:'phone.png'}})};
            if (String(url).startsWith('/von/generate')) {
                bodies.push(JSON.parse(options.body));
                return {ok:true, json:async () => ({response:'Image received.', llm_debug:{model:'test-model'}})};
            }
            if (String(url).startsWith('/von/history/length')) return {ok:true, json:async () => ({history_length:0, authenticated:true})};
            if (String(url).startsWith('/von/api/render_markdown')) return {ok:true, json:async () => ({html:JSON.parse(options.body).text || ''})};
            return {ok:true, json:async () => ({})};
        });
        await __testOnly_uploadFilesToVon([new File(['png'], 'phone.png')]);
        expect(document.querySelector('#conversationImageComposer img')).not.toBeNull();
        document.querySelector('#conversationImageComposer button[aria-label^=Remove]').click();
        await __testOnly_uploadFilesToVon([new File(['png'], 'replacement.png')]);
        document.querySelector('#promptInput').value = 'Describe this screenshot';
        await sendMessage();
        expect(bodies[0].image_attachment_ids).toEqual(['#V#phone-2']);
        expect(bodies[0]).not.toHaveProperty('workflow_inputs');
        document.querySelector('#promptInput').value = 'Now a text-only question';
        await sendMessage();
        expect(bodies[1]).not.toHaveProperty('image_attachment_ids');
        expect(fetch.mock.calls.some(([url]) => url === '/von/api/files/upload')).toBe(false);
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
        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Represent the PhD students and supervision information.';
        await __testOnly_uploadFilesToVon([uploadedFile]);

        expect(promptInput.value).toBe(
            'Represent the PhD students and supervision information.'
        );
        expect(promptInput.value).not.toContain('supervision.xlsx');
        expect(promptInput.value).not.toContain(fileCopyConceptId);
        const pendingAttachmentStatus = document.getElementById(
            'pendingAttachmentStatus'
        );
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(false);
        expect(pendingAttachmentStatus.textContent).toContain('supervision.xlsx');

        await sendMessage();

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
        expect(generateBodies[0].prompt).not.toContain(fileCopyConceptId);
        expect(generateBodies[0].prompt).not.toContain('supervision.xlsx');
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(true);

        promptInput.value = 'A separate follow-up without an attachment.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[1]).not.toHaveProperty('workflow_inputs');
    }, 15000);

    test('a slow upload exposes progress and coalesces repeated attachment actions', async () => {
        const { __testOnly_uploadFilesToVon } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_slow_guard_test';
        let resolveUpload;
        let uploadFetchCount = 0;
        const uploadResponse = new Promise((resolve) => {
            resolveUpload = resolve;
        });

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                uploadFetchCount += 1;
                return uploadResponse;
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
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(
            ['candidate,supervisor\nExample,Professor Example\n'],
            'slow-supervision.xlsx',
            {
                type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                lastModified: 1785167279463
            }
        );
        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Keep this draft exactly as written.';

        const firstUpload = __testOnly_uploadFilesToVon([uploadedFile]);
        await Promise.resolve();
        const repeatedUpload = __testOnly_uploadFilesToVon([uploadedFile]);

        expect(repeatedUpload).toBe(firstUpload);
        expect(uploadFetchCount).toBe(1);
        expect(promptInput.value).toBe('Keep this draft exactly as written.');
        expect(document.getElementById('chatAttachmentStatus').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('uploadFileStatus').textContent).toContain(
            'already in progress'
        );
        expect(document.getElementById('uploadFileButton').disabled).toBe(true);

        resolveUpload({
            ok: true,
            json: async () => ({
                success: true,
                uploaded: {
                    concept_id: fileCopyConceptId,
                    type_concept_id: '#V#computer_file_copy'
                },
                storage: {
                    backend: 'test',
                    key: 'uploads/test/slow-supervision.xlsx'
                },
                chat_history_recorded: true
            })
        });
        await Promise.all([firstUpload, repeatedUpload]);
        await Promise.resolve();

        expect(uploadFetchCount).toBe(1);
        expect(promptInput.value).toBe('Keep this draft exactly as written.');
        expect(document.getElementById('uploadFileButton').disabled).toBe(false);
        expect(document.getElementById('pendingAttachmentStatus').textContent).toContain(
            'slow-supervision.xlsx'
        );
        const confirmationCount = (
            document.getElementById('scrollableField').textContent.match(
                /File uploaded and registered as/g
            ) || []
        ).length;
        expect(confirmationCount).toBe(1);
    }, 15000);

    test('send stays blocked until a slow upload can bind its file-copy id', async () => {
        const {
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_slow_send_test';
        const generateBodies = [];
        let resolveUpload;
        const uploadResponse = new Promise((resolve) => {
            resolveUpload = resolve;
        });

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return uploadResponse;
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
                        response: 'Meeting workflow started.',
                        llm_debug: { model: 'test-model' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(
            ['BEGIN:VCALENDAR\nEND:VCALENDAR\n'],
            'slow-meeting.ics',
            { type: 'text/calendar' }
        );
        const promptInput = document.getElementById('promptInput');
        const sendButton = document.getElementById('sendButton');
        promptInput.value = 'Represent this meeting.';

        const upload = __testOnly_uploadFilesToVon([uploadedFile]);
        await Promise.resolve();

        expect(sendButton.disabled).toBe(true);
        expect(sendButton.title).toBe(
            'Wait for the attachment upload to finish before sending.'
        );

        // `sendMessage` is the same central handler used by both the button and
        // the unshifted Enter shortcut. It must not construct an unbound turn.
        await sendMessage();

        expect(generateBodies).toHaveLength(0);
        expect(promptInput.value).toBe('Represent this meeting.');
        expect(document.body.textContent).toContain(
            'Wait for the attachment upload to finish before sending.'
        );

        resolveUpload({
            ok: true,
            json: async () => ({
                success: true,
                uploaded: {
                    concept_id: fileCopyConceptId,
                    type_concept_id: '#V#computer_file_copy'
                },
                storage: {
                    backend: 'test',
                    key: 'uploads/test/slow-meeting.ics'
                },
                chat_history_recorded: true
            })
        });
        await upload;
        await Promise.resolve();

        expect(sendButton.disabled).toBe(false);
        expect(sendButton.title).toBe('Send Prompt');

        await sendMessage();

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);

    test('uploads independently in two conversations without discarding either file', async () => {
        const {
            __testOnly_setActiveChatSession,
            __testOnly_uploadFilesToVon
        } = require(chatTabModulePath);
        const deferredUploads = new Map();
        const uploadFileNames = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                const fileName = options.body.get('file').name;
                uploadFileNames.push(fileName);
                return new Promise((resolve) => {
                    deferredUploads.set(fileName, resolve);
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const fileA = new File(['A'], 'conversation-a.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        });
        const fileB = new File(['B'], 'conversation-b.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        });

        const uploadA = __testOnly_uploadFilesToVon([fileA]);
        await Promise.resolve();
        __testOnly_setActiveChatSession('conversation-b', 'Conversation B');
        expect(document.getElementById('uploadFileButton').disabled).toBe(false);

        const uploadB = __testOnly_uploadFilesToVon([fileB]);
        await Promise.resolve();

        expect(uploadB).not.toBe(uploadA);
        expect(uploadFileNames).toEqual([
            'conversation-a.xlsx',
            'conversation-b.xlsx'
        ]);

        deferredUploads.get('conversation-b.xlsx')({
            ok: true,
            json: async () => ({
                success: true,
                uploaded: {
                    concept_id: '#V#uploaded_file_copy_conversation_b',
                    type_concept_id: '#V#computer_file_copy'
                },
                storage: {
                    backend: 'test',
                    key: 'uploads/test/conversation-b.xlsx'
                },
                chat_history_recorded: true
            })
        });
        await uploadB;
        expect(document.getElementById('pendingAttachmentStatus').textContent).toContain(
            'conversation-b.xlsx'
        );

        deferredUploads.get('conversation-a.xlsx')({
            ok: true,
            json: async () => ({
                success: true,
                uploaded: {
                    concept_id: '#V#uploaded_file_copy_conversation_a',
                    type_concept_id: '#V#computer_file_copy'
                },
                storage: {
                    backend: 'test',
                    key: 'uploads/test/conversation-a.xlsx'
                },
                chat_history_recorded: true
            })
        });
        await uploadA;

        expect(document.getElementById('pendingAttachmentStatus').textContent).toContain(
            'conversation-b.xlsx'
        );
        __testOnly_setActiveChatSession('attachment-session', 'Attachment test');
        expect(document.getElementById('pendingAttachmentStatus').textContent).toContain(
            'conversation-a.xlsx'
        );
    }, 15000);

    test('does not leak upload progress or completion into another conversation', async () => {
        const {
            __testOnly_setActiveChatSession,
            __testOnly_uploadFilesToVon
        } = require(chatTabModulePath);
        let resolveUpload;
        const uploadResponse = new Promise((resolve) => {
            resolveUpload = resolve;
        });

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/files/upload') {
                return uploadResponse;
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const uploadedFile = new File(['A'], 'stay-in-a.xlsx', {
            type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        });
        const upload = __testOnly_uploadFilesToVon([uploadedFile]);
        await Promise.resolve();
        expect(document.getElementById('uploadFileStatus').textContent).toContain(
            'stay-in-a.xlsx'
        );

        __testOnly_setActiveChatSession('conversation-b', 'Conversation B');
        document.getElementById('scrollableField').textContent = '';
        expect(document.getElementById('uploadFileStatus').textContent).toBe('');
        expect(document.getElementById('uploadFileButton').disabled).toBe(false);

        resolveUpload({
            ok: true,
            json: async () => ({
                success: true,
                uploaded: {
                    concept_id: '#V#uploaded_file_copy_stay_in_a',
                    type_concept_id: '#V#computer_file_copy'
                },
                storage: {
                    backend: 'test',
                    key: 'uploads/test/stay-in-a.xlsx'
                },
                chat_history_recorded: true
            })
        });
        await upload;

        expect(document.getElementById('uploadFileStatus').textContent).toBe('');
        expect(document.getElementById('pendingAttachmentStatus').classList.contains('hidden')).toBe(true);
        expect(document.getElementById('scrollableField').textContent).not.toContain(
            'File uploaded and registered as'
        );

        __testOnly_setActiveChatSession('attachment-session', 'Attachment test');
        expect(document.getElementById('uploadFileStatus').textContent).toContain(
            'Upload complete'
        );
        expect(document.getElementById('pendingAttachmentStatus').textContent).toContain(
            'stay-in-a.xlsx'
        );
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
        const pendingAttachmentStatus = document.getElementById(
            'pendingAttachmentStatus'
        );
        promptInput.value = 'Represent this spreadsheet.';
        await sendMessage();
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(false);
        expect(pendingAttachmentStatus.textContent).toContain('retry.xlsx');

        promptInput.value = 'Retry the spreadsheet representation.';
        await sendMessage();
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(true);

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
        const pendingAttachmentStatus = document.getElementById(
            'pendingAttachmentStatus'
        );
        expect(pendingAttachmentStatus.textContent).toContain('session.xlsx');
        __testOnly_setActiveChatSession('unrelated-session', 'Unrelated');
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(true);
        promptInput.value = 'An unrelated question.';
        await sendMessage();

        __testOnly_setActiveChatSession('attachment-session', 'Attachment test');
        expect(pendingAttachmentStatus.classList.contains('hidden')).toBe(false);
        expect(pendingAttachmentStatus.textContent).toContain('session.xlsx');
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
                        session_name: null,
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
            null
        );
        promptInput.value = 'Represent the uploaded spreadsheet.';
        await sendMessage();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[0]).not.toHaveProperty('workflow_inputs');
        expect(generateBodies[1].workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);

    test('server-queued prompts carry only the attachment bound to that prompt', async () => {
        const {
            __testOnly_uploadFilesToVon,
            sendMessage
        } = require(chatTabModulePath);
        const fileCopyConceptId = '#V#uploaded_file_copy_attachment_queue_test';
        const generateBodies = [];
        const queuePostBodies = [];
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
                queuePostBodies.push(body);
                queueCounter += 1;
                const queueId = `queue-${queueCounter}`;
                queueRecords.set(queueId, {
                    ...body,
                    queue_id: queueId,
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

        for (let attempt = 0; attempt < 40 && queuePostBodies.length < 3; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 10));
        }

        // Only the foreground turn is browser-owned. The two deferred turns
        // remain durable server-dispatch rows, including their frozen and
        // prompt-specific attachment envelopes.
        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0]).not.toHaveProperty('workflow_inputs');
        expect(queuePostBodies).toHaveLength(3);
        expect(queuePostBodies[0]).not.toHaveProperty('execution_envelope');
        expect(queuePostBodies[1].execution_envelope).not.toHaveProperty('workflow_inputs');
        expect(queuePostBodies[2].execution_envelope.workflow_inputs).toEqual({
            file_copy_concept_id: fileCopyConceptId
        });
    }, 15000);
});
