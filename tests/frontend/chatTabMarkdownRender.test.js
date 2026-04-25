/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

const { sendMessage } = require(chatTabModulePath);

function mockCommonChatSessionResponse(url) {
    if (typeof url !== 'string') {
        return null;
    }

    if (url.startsWith('/von/api/session/create_chat_session')) {
        return Promise.resolve({
            ok: true,
            json: async () => ({
                session_id: 'test-chat-session',
                session_name: 'Test chat session',
                history: []
            })
        });
    }

    if (url.startsWith('/von/api/session/set_chat_session')) {
        return Promise.resolve({
            ok: true,
            json: async () => ({
                session_id: 'test-chat-session',
                session_name: 'Test chat session'
            })
        });
    }

    if (url.startsWith('/von/api/session/chat_session_links')) {
        return Promise.resolve({
            ok: true,
            json: async () => ({ links: [] })
        });
    }

    return null;
}

describe('chat markdown rendering (assistant)', () => {
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
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('renders markdown for GPT-5.2 responses safely and preserves #V# token linkification (including standalone fenced IDs)', async () => {
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
            'Inline code token: `#V#yejin_choi`',
            '',
            '[good](https://example.com)',
            '',
            'See #V#person',
            '',
            'Grounding: #V#michael_witbrock #V#author_of #V#learning_to_tell_two_spirals_apart',
            '',
            '```',
            '#V#person',
            '```',
            '',
            '```',
            '#V#person',
            '#V#thing',
            '```',
            '',
            '[bad](javascript:alert(1))',
            '',
            '<script>window.hacked=1</script>'
        ].join('\n');

        const assistantHtml = [
            '<h1>Title</h1>',
            '<ul><li>Item</li></ul>',
            '<p>Inline code token: <code>#V#yejin_choi</code></p>',
            '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer">good</a></p>',
            '<p>See #V#person</p>',
            '<p>Grounding: #V#michael_witbrock #V#author_of #V#learning_to_tell_two_spirals_apart</p>',
            '<pre><code>#V#person</code></pre>',
            '<pre><code>#V#person\n#V#thing</code></pre>',
            '<p>bad</p>'
        ].join('\n');

        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                if (url.includes('identifier=%23V%23yejin_choi')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ display_name: 'Yejin Choi', kind: 'individual' })
                    });
                }
                if (url.includes('identifier=%23V%23author_of')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ display_name: 'Author of', kind: 'predicate' })
                    });
                }
                if (url.includes('identifier=%23V%23michael_witbrock')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ display_name: 'MJW', kind: 'individual' })
                    });
                }
                if (url.includes('identifier=%23V%23learning_to_tell_two_spirals_apart')) {
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({ display_name: 'Learning To Tell Two Spirals Apart', kind: 'individual' })
                    });
                }
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

        const assistantHeader = assistantMarkdown.closest('.message-container')?.querySelector('div');
        const renderBadge = assistantHeader?.querySelector('.chat-render-mode-badge');
        expect(renderBadge).toBeTruthy();
        expect(renderBadge.textContent).toContain('View: Rendered');

        // Chat transcript should not inherit centring/boldness from surrounding containers.
        expect(assistantMarkdown.style.textAlign).toBe('left');
        expect(assistantMarkdown.style.fontWeight).toBe('400');

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

        const assertion = assistantMarkdown.querySelector('.vontology-inline-assertion');
        expect(assertion).not.toBeNull();
        expect(assertion.dataset.subjectConceptId).toBe('#V#michael_witbrock');
        expect(assertion.dataset.predicateConceptId).toBe('#V#author_of');
        expect(assertion.dataset.objectConceptId).toBe('#V#learning_to_tell_two_spirals_apart');
        const assertionCartouches = assertion.querySelectorAll('.vontology-cartouche[data-full-concept-id]');
        expect(assertionCartouches).toHaveLength(3);
        expect(assertionCartouches[1].dataset.assertionRole).toBe('predicate');

        // Inline code tokens should become cartouches, but only when the <code> span contains exactly the token.
        const codeCartouche = assistantMarkdown.querySelector('.vontology-cartouche.vontology-cartouche-inline-code[data-full-concept-id="#V#yejin_choi"]');
        expect(codeCartouche).not.toBeNull();

        // Allow async hydration to resolve (fetch + microtasks).
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(cartouche.textContent).toContain('Person');
        expect(cartouche.textContent).toContain('#V#person');
        expect(cartouche.textContent).toContain('Type');
        expect(assertionCartouches[1].textContent).toContain('Author of');
        expect(assertionCartouches[1].textContent).toContain('Predicate');

        expect(codeCartouche.textContent).toContain('#V#yejin_choi');

        // A fenced code block containing only a single concept ID becomes a cartouche.
        const codeBlockCartouche = assistantMarkdown.querySelector('.vontology-cartouche-block .vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(codeBlockCartouche).not.toBeNull();

        // But real/multi-line code blocks remain untouched.
        const codeBlocks = Array.from(assistantMarkdown.querySelectorAll('pre'));
        expect(codeBlocks.length).toBeGreaterThanOrEqual(1);
        const multiLine = codeBlocks.find((el) => (el.textContent || '').includes('#V#thing'));
        expect(multiLine).toBeTruthy();
        expect(multiLine.querySelector('.vontology-cartouche')).toBeNull();
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

        global.fetch = jest.fn((url, _opts) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

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

        // Chat transcript should not inherit centring/boldness from surrounding containers.
        expect(userMarkdown.style.textAlign).toBe('left');
        expect(userMarkdown.style.fontWeight).toBe('400');
        expect(userMarkdown.querySelector('h1')).not.toBeNull();
        expect(userMarkdown.textContent).toContain('Title');

        const cartouche = userMarkdown.querySelector('.vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(cartouche).not.toBeNull();
    });

    test('recovers when rendered HTML drops leading # (V#person) and still cartouchifies standalone fenced IDs', async () => {
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
            '- State A',
            '  - #V#person',
            '',
            '```',
            '#V#person',
            '```'
        ].join('\n');

        const assistantHtml = [
            '<ul><li>State A<ul><li>V#person</li></ul></li></ul>',
            '<pre><code>V#person</code></pre>'
        ].join('\n');

        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

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
                    json: async () => ({ html: assistantHtml })
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
                    json: async () => ({ response: assistantResponse, llm_debug: { model: 'gpt-5.2' } })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const assistantMarkdown = scrollableField.querySelector('.chat-markdown.markdown-rendered');
        expect(assistantMarkdown).not.toBeNull();

        // The list item should become a cartouche even though input was V#person.
        const cartouche = assistantMarkdown.querySelector('.vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(cartouche).not.toBeNull();

        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(cartouche.textContent).toContain('Person');
        expect(cartouche.textContent).toContain('#V#person');

        // Standalone fenced IDs should cartouchify even if the leading '#' was dropped.
        const fencedCartouche = assistantMarkdown.querySelector('.vontology-cartouche-block .vontology-cartouche[data-full-concept-id="#V#person"]');
        expect(fencedCartouche).not.toBeNull();
    });

    test('turns quoted bold blockquotes into insert buttons for the chat prompt', async () => {
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
            'Do this:',
            '',
            '> **“Apply the hierarchy fix.”**',
            '> **“Create `work_specification` and re-anchor `task_specification`.”**',
            '> **“Do the thing.”** now',
            '> **Apply the hierarchy fix.**'
        ].join('\n');

        const assistantHtml = [
            '<p>Do this:</p>',
            '<blockquote><p><strong>“Apply the hierarchy fix.”</strong></p></blockquote>',
            '<blockquote><p><strong>“Create <code>work_specification</code> and re-anchor <code>task_specification</code>.”</strong></p></blockquote>',
            // Should NOT match: extra text outside <strong>.
            '<blockquote><p><strong>“Do the thing.”</strong> now</p></blockquote>',
            // Should NOT match: missing quotes.
            '<blockquote><p><strong>Apply the hierarchy fix.</strong></p></blockquote>'
        ].join('\n');

        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: assistantHtml })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ results: [] })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ response: assistantResponse, llm_debug: { model: 'gpt-5.2' } })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const assistantMarkdown = scrollableField.querySelector('.chat-markdown.markdown-rendered');
        expect(assistantMarkdown).not.toBeNull();

        const buttons = Array.from(assistantMarkdown.querySelectorAll('button.chat-insert-prompt-button'));
        expect(buttons.length).toBe(2);

        const simpleButton = buttons.find((b) => (b.textContent || '').includes('Apply the hierarchy fix.'));
        expect(simpleButton).toBeTruthy();

        const codeButton = buttons.find((b) => (b.textContent || '').includes('work_specification'));
        expect(codeButton).toBeTruthy();
        expect(codeButton.textContent).toContain('`work_specification`');
        expect(codeButton.textContent).toContain('`task_specification`');

        const remainingBlockquotes = Array.from(assistantMarkdown.querySelectorAll('blockquote'));
        expect(remainingBlockquotes.length).toBe(2);

        // Make insertion deterministic.
        promptInput.value = 'Alpha';
        promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);

        simpleButton.click();

        expect(promptInput.value).toBe('Alpha\nApply the hierarchy fix.');
    });

    test('buttonifies "just say" replies (Proceed/yes/etc) into quick-reply buttons, but never inside code blocks', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        // sendMessage() requires a non-empty prompt; it will clear the input after sending.
        promptInput.value = 'seed prompt';
        promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);

        const assistantResponse = [
            'To continue, just say “Proceed”.',
            'If you agree, just say "yes".',
            'Otherwise, just say no.',
            'For the next step, say “Proceed with authors” or “Proceed with full enrichment”.',
            '```',
            'just say “Proceed”',
            '```'
        ].join('\n');

        const assistantHtml = [
            '<p>To continue, just say “Proceed”.</p>',
            '<p>If you agree, just say <strong>“yes”</strong>.</p>',
            '<p>Otherwise, just say no.</p>',
            '<p>For the next step, say <strong>“Proceed with authors”</strong> or <strong>“Proceed with full enrichment”</strong>.</p>',
            '<pre><code>just say “Proceed”</code></pre>'
        ].join('\n');

        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: assistantHtml })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ results: [] })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ response: assistantResponse, llm_debug: { model: 'gpt-5.2' } })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const assistantMarkdown = scrollableField.querySelector('.chat-markdown.markdown-rendered');
        expect(assistantMarkdown).not.toBeNull();

        const buttons = Array.from(assistantMarkdown.querySelectorAll('button.chat-insert-prompt-button'));
        expect(buttons.length).toBe(5);

        const proceedButton = buttons.find((b) => (b.textContent || '') === 'Proceed');
        expect(proceedButton).toBeTruthy();

        const yesButton = buttons.find((b) => (b.textContent || '') === 'yes');
        expect(yesButton).toBeTruthy();

        const noButton = buttons.find((b) => (b.textContent || '') === 'no');
        expect(noButton).toBeTruthy();

        const proceedAuthorsButton = buttons.find((b) => (b.textContent || '') === 'Proceed with authors');
        expect(proceedAuthorsButton).toBeTruthy();

        const proceedFullButton = buttons.find((b) => (b.textContent || '') === 'Proceed with full enrichment');
        expect(proceedFullButton).toBeTruthy();

        const codeBlock = assistantMarkdown.querySelector('pre');
        expect(codeBlock).toBeTruthy();
        expect(codeBlock.querySelector('button')).toBeNull();

        // Make insertion deterministic. (sendMessage clears the prompt input.)
        promptInput.value = 'Alpha';
        promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);

        proceedButton.click();
        expect(promptInput.value).toBe('Alpha\nProceed');
    });

    test('renders assistant pipe tables for GPT-5.4-family responses and supports raw-text toggle', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'show me the table';

        const assistantResponse = [
            '| Name | Degree | Role |',
            '| --- | --- | --- |',
            '| Alice | PhD | Student |',
            '| Bob | MSc | Tutor |'
        ].join('\n');

        const assistantHtml = [
            '<table>',
            '<thead><tr><th>Name</th><th>Degree</th><th>Role</th></tr></thead>',
            '<tbody>',
            '<tr><td>Alice</td><td>PhD</td><td>Student</td></tr>',
            '<tr><td>Bob</td><td>MSc</td><td>Tutor</td></tr>',
            '</tbody>',
            '</table>'
        ].join('');

        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
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
                        llm_debug: { model: 'gpt-5.4-mini-2026-03-17' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        const assistantMarkdown = scrollableField.querySelector('.chat-markdown.markdown-rendered');
        expect(assistantMarkdown).not.toBeNull();
        expect(assistantMarkdown.querySelector('table')).not.toBeNull();
        expect(assistantMarkdown.textContent).toContain('Alice');

        const assistantContainer = assistantMarkdown.closest('.message-container');
        const toggleButton = assistantContainer.querySelector('.chat-render-toggle');
        const renderBadge = assistantContainer.querySelector('.chat-render-mode-badge');
        expect(toggleButton).toBeTruthy();
        expect(toggleButton.textContent).toBe('Text');
        expect(renderBadge.textContent).toContain('View: Rendered');

        toggleButton.click();

        expect(assistantMarkdown.dataset.renderMode).toBe('text');
        expect(assistantMarkdown.classList.contains('chat-markdown')).toBe(false);
        expect(assistantMarkdown.querySelector('table')).toBeNull();
        expect(assistantMarkdown.textContent).toContain('| Name | Degree | Role |');
        expect(renderBadge.textContent).toContain('View: Text');
        expect(toggleButton.textContent).toBe('Rendered');

        toggleButton.click();

        expect(assistantMarkdown.dataset.renderMode).toBe('rendered');
        expect(assistantMarkdown.classList.contains('chat-markdown')).toBe(true);
        expect(assistantMarkdown.querySelector('table')).not.toBeNull();
        expect(renderBadge.textContent).toContain('View: Rendered');
        expect(toggleButton.textContent).toBe('Text');
    });

    test('keeps GPT-5.4-family plain-text assistant responses in text mode', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'plain response please';

        let renderMarkdownCalls = 0;
        global.fetch = jest.fn((url) => {
            const common = mockCommonChatSessionResponse(url);
            if (common) return common;

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                renderMarkdownCalls += 1;
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: '<p>Should not be used</p>' })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'This is a plain assistant reply with no markdown markers.',
                        llm_debug: { model: 'gpt-5.4-mini-2026-03-17' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField.querySelector('.chat-render-toggle')).toBeNull();
        expect(scrollableField.querySelector('.chat-markdown.markdown-rendered')).toBeNull();
        expect(scrollableField.textContent).toContain('This is a plain assistant reply with no markdown markers.');
        expect(renderMarkdownCalls).toBe(0);
    });
});
