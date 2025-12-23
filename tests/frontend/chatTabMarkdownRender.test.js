/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

const { sendMessage } = require(chatTabModulePath);

describe('chat markdown rendering (assistant)', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div id="loadingIndicator" aria-hidden="true"></div>
            <button id="sendButton"></button>
            <button id="abortButton" aria-hidden="true"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('renders markdown for GPT-5.2 responses safely and preserves #V# token linkification (not inside code blocks)', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        const assistantResponse = [
            '# Title',
            '',
            '- Item',
            '',
            '[good](https://example.com)',
            '',
            'See #V#person',
            '',
            '```',
            '#V#person',
            '```',
            '',
            '[bad](javascript:alert(1))',
            '',
            '<script>window.hacked=1</script>'
        ].join('\n');

        const assistantHtml = [
            '<h1>Title</h1>',
            '<ul><li>Item</li></ul>',
            '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer">good</a></p>',
            '<p>See #V#person</p>',
            '<pre><code>#V#person</code></pre>',
            '<p>bad</p>'
        ].join('\n');

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ display_name: 'Person', kind: 'type' })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        results: [
                            { id: '#V#person', name: 'Person', kind: 'type' }
                        ]
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: assistantHtml })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: assistantResponse,
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        // Markdown rendering is now async (server-backed), so allow a tick.
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const assistantMarkdown = scrollableField.querySelector('.chat-markdown.markdown-rendered');
        expect(assistantMarkdown).not.toBeNull();

        expect(assistantMarkdown.querySelector('h1')).not.toBeNull();
        expect(assistantMarkdown.querySelector('ul')).not.toBeNull();
        expect(assistantMarkdown.textContent).toContain('Title');
        expect(assistantMarkdown.textContent).toContain('Item');

        // XSS hardening: scripts should not be injected.
        expect(assistantMarkdown.querySelector('script')).toBeNull();
        expect(global.window.hacked).toBeUndefined();

        // XSS hardening: javascript: links should not appear.
        const hrefs = Array.from(assistantMarkdown.querySelectorAll('a'))
            .map(a => (a.getAttribute('href') || '').toLowerCase());
        expect(hrefs.some(h => h.startsWith('javascript:'))).toBe(false);

        // External http(s) links open in a new tab.
        const externalLink = Array.from(assistantMarkdown.querySelectorAll('a'))
            .find(a => (a.getAttribute('href') || '') === 'https://example.com');
        expect(externalLink).toBeTruthy();
        expect(externalLink.getAttribute('target')).toBe('_blank');

        // #V# token cartouches should be present in normal text.
        const cartouche = assistantMarkdown.querySelector('.vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(cartouche).not.toBeNull();

        // Allow async hydration to resolve (fetch + microtasks).
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(cartouche.textContent).toContain('Person');
        expect(cartouche.textContent).toContain('#V#person');
        expect(cartouche.textContent).toContain('Type');

        // But must not linkify inside code blocks.
        const codeBlock = assistantMarkdown.querySelector('pre');
        expect(codeBlock).not.toBeNull();
        expect(codeBlock.querySelector('.vontology-cartouche')).toBeNull();
        expect(codeBlock.textContent).toContain('#V#person');
    });

    test('renders markdown for user messages when markdown is detected', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = [
            '# Title',
            '',
            '- Item',
            '',
            'See #V#person'
        ].join('\n');

        const renderedHtml = [
            '<h1>Title</h1>',
            '<ul><li>Item</li></ul>',
            '<p>See #V#person</p>'
        ].join('\n');

        global.fetch = jest.fn((url, opts) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ display_name: 'Person', kind: 'type' })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: renderedHtml })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ results: [{ id: '#V#person', name: 'Person', kind: 'type' }] })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ response: 'ok', llm_debug: { model: 'gpt-5.2' } })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const messageContainers = Array.from(scrollableField.querySelectorAll('.message-container'));
        const userContainer = messageContainers.find(el => (el.textContent || '').includes('User •'));
        expect(userContainer).toBeTruthy();

        const userMarkdown = userContainer.querySelector('.chat-markdown.markdown-rendered');
        expect(userMarkdown).not.toBeNull();
        expect(userMarkdown.querySelector('h1')).not.toBeNull();
        expect(userMarkdown.textContent).toContain('Title');

        const cartouche = userMarkdown.querySelector('.vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(cartouche).not.toBeNull();
    });
});
