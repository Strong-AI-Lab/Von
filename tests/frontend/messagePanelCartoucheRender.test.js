/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJson: jest.fn(),
    postJson: jest.fn(),
    postJsonDetailed: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn()
}));

const modulePath = '../../src/frontend/web/von_interface/static/js/components/messagePanel.js';

function flushUi() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('message panel concept cartouches', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="messagesContainer"></div>
            <span id="unreadMessageBadge" class="hidden"></span>
        `;
    });

    afterEach(() => {
        jest.resetModules();
        jest.clearAllMocks();
        delete global.fetch;
    });

    test('renders message-body `#V#` references as hydrated cartouches that preserve normal concept selection behaviour', async () => {
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);

        postJson.mockResolvedValue({});
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return {
                    threads: [{
                        _id: ['#V#von_system'],
                        last_message: {
                            concept_data: {
                                content_fallback: 'Hello #V#michael_witbrock'
                            }
                        },
                        message_count: 1
                    }]
                };
            }
            if (url === '/api/messages/unread/count') {
                return { unread_count: 0 };
            }
            if (url === '/api/messages/conversation/%23V%23von_system?limit=50') {
                return {
                    messages: [{
                        concept_id: '#V#message_1',
                        relationships: {
                            '#V#has_sender': ['#V#von_system']
                        },
                        concept_data: {
                            content_fallback: 'Hello #V#michael_witbrock,\nPlease review #V#paper_bench_starace_et_al2025.'
                        },
                        created_at: '2026-04-05T04:28:00Z'
                    }]
                };
            }
            throw new Error(`Unexpected getJson call: ${url}`);
        });

        global.fetch = jest.fn(async (url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('raw_only=1') && url.includes('identifier=%23V%23michael_witbrock')) {
                    return {
                        ok: true,
                        status: 200,
                        json: async () => ({
                            display_name: 'Michael Witbrock',
                            kind: 'individual',
                            raw_doc: {
                                names: [{ name: 'Michael Witbrock', language: 'en-NZ', type: 'NL' }]
                            }
                        })
                    };
                }
                if (url.includes('raw_only=1') && url.includes('identifier=%23V%23paper_bench_starace_et_al2025')) {
                    return {
                        ok: true,
                        status: 200,
                        json: async () => ({
                            display_name: 'Paper Bench Starace Et Al2025',
                            kind: 'individual',
                            raw_doc: {
                                names: [{ name: 'Paper Bench Starace Et Al2025', language: 'en-NZ', type: 'NL' }]
                            }
                        })
                    };
                }
                return {
                    ok: true,
                    status: 200,
                    text: async () => JSON.stringify({})
                };
            }
            throw new Error(`Unexpected fetch call: ${url}`);
        });

        initializeMessagePanel();
        await showMessagesTab();

        const threadItem = document.querySelector('.message-thread-item');
        expect(threadItem).not.toBeNull();

        threadItem.click();
        await flushUi();

        const contentHost = document.querySelector('#messageViewContent');
        const cartouches = Array.from(contentHost.querySelectorAll('.vontology-cartouche[data-full-concept-id]'));
        expect(cartouches.map((cartouche) => cartouche.dataset.fullConceptId)).toEqual([
            '#V#michael_witbrock',
            '#V#paper_bench_starace_et_al2025'
        ]);

        expect(cartouches[0].textContent).toContain('Michael Witbrock');
        expect(cartouches[0].textContent).toContain('Individual');

        const seen = [];
        document.addEventListener('von:selectConceptById', (event) => {
            seen.push(event.detail);
        });

        cartouches[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(seen).toHaveLength(1);
        expect(seen[0]).toMatchObject({
            conceptId: 'michael_witbrock',
            createConceptTab: true,
            promoteExistingTab: true
        });
    });

    test('offers a separate Discuss control for an individual message', async () => {
        const { getJson, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { initializeMessagePanel, showMessagesTab } = require(modulePath);
        postJson.mockResolvedValue({});
        getJson.mockImplementation(async (url) => {
            if (url === '/api/messages/threads?limit=20') {
                return { threads: [{ _id: ['#V#von_system'], last_message: {}, message_count: 1 }] };
            }
            if (url === '/api/messages/unread/count') return { unread_count: 0 };
            if (url === '/api/messages/conversation/%23V%23von_system?limit=50') {
                return {
                    messages: [{
                        concept_id: '#V#message_2675',
                        relationships: { '#V#has_sender': ['#V#von_system'] },
                        concept_data: { content_fallback: 'Discuss this message.' },
                        created_at: '2026-08-22T10:00:00Z',
                    }],
                };
            }
            return {};
        });

        initializeMessagePanel();
        await showMessagesTab();
        document.querySelector('.message-thread-item').click();
        await flushUi();

        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener('von:discussConcept', handler);
        document.querySelector('.message-discuss-btn').click();

        expect(seen).toEqual([{
            conceptId: '#V#message_2675',
            conceptName: 'Message',
            source: 'message',
        }]);
        document.removeEventListener('von:discussConcept', handler);
    });
});
