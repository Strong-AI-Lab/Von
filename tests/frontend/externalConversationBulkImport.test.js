/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#test_user', org_id: '#V#test_org' })),
    getWindowSessionId: jest.fn(() => 'bulk-import-window'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#test_user'),
    renderSpanSuggestions: jest.fn()
}));

describe('durable machine conversation import UI', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = `
            <section id="externalConversationBulkImportPanel" class="hidden">
                <strong id="externalConversationBulkImportTitle"></strong>
                <div id="externalConversationBulkImportSummary"></div>
                <button id="pauseExternalConversationBulkImportBtn">Pause</button>
                <button id="resumeExternalConversationBulkImportBtn" class="hidden">Resume</button>
                <button id="cancelExternalConversationBulkImportBtn">Cancel</button>
            </section>
        `;
        window.confirm = jest.fn(() => true);
    });

    afterEach(() => {
        require(chatTabModulePath).__testOnly_resetExternalConversationBulkImport();
        jest.useRealTimers();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('renders durable progress, pause, and reconnect state', () => {
        const { __testOnly_renderExternalConversationBulkImport } = require(chatTabModulePath);
        const batch = {
            batch_id: 'batch-1',
            status: 'running',
            source_count: 10,
            counts: { completed: 4, failed: 1, unsupported: 1 }
        };

        __testOnly_renderExternalConversationBulkImport(batch);
        expect(document.getElementById('externalConversationBulkImportPanel').classList.contains('hidden')).toBe(false);
        expect(document.getElementById('externalConversationBulkImportSummary').textContent).toContain('6 of 10');
        expect(document.getElementById('pauseExternalConversationBulkImportBtn').classList.contains('hidden')).toBe(false);

        __testOnly_renderExternalConversationBulkImport({ ...batch, status: 'paused' });
        expect(document.getElementById('resumeExternalConversationBulkImportBtn').classList.contains('hidden')).toBe(false);

        __testOnly_renderExternalConversationBulkImport(batch, { reconnecting: true });
        expect(document.getElementById('externalConversationBulkImportSummary').textContent).toContain('continues on the server');
    });

    test('previews before starting and sends providers without a client path', async () => {
        const requests = [];
        global.fetch = jest.fn(async (url, options = {}) => {
            requests.push({ url, options });
            if (String(url).endsWith('/preview')) {
                return {
                    ok: true,
                    json: async () => ({
                        source_count: 2,
                        providers: {
                            codex: { source_count: 1 },
                            gemini: { source_count: 1 }
                        }
                    })
                };
            }
            return {
                ok: true,
                json: async () => ({
                    batch_id: 'batch-1',
                    status: 'ready',
                    source_count: 2,
                    counts: { pending: 2 }
                })
            };
        });
        const { __testOnly_startExternalConversationBulkImport } = require(chatTabModulePath);

        await __testOnly_startExternalConversationBulkImport();

        expect(requests.map((request) => request.url)).toEqual([
            '/von/api/session/external_conversation_import/preview',
            '/von/api/session/external_conversation_import/batches'
        ]);
        const startedBody = JSON.parse(requests[1].options.body);
        expect(startedBody.providers).toEqual(['codex', 'claude_code', 'copilot', 'gemini']);
        expect(JSON.stringify(startedBody)).not.toContain('path');
        expect(document.getElementById('externalConversationBulkImportTitle').textContent).toContain('ready');
    });
});
