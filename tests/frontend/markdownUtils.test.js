/** @jest-environment jsdom */

const { detectMarkdown, simpleMarkdownToHtml } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');

describe('markdownUtils', () => {
    test('detectMarkdown recognises GitHub-style pipe tables', () => {
        const input = [
            '| Name | Degree | Role |',
            '| --- | --- | --- |',
            '| Alice | PhD | Student |'
        ].join('\n');

        expect(detectMarkdown(input)).toBe(true);
    });

    test('detectMarkdown recognises indented list markers', () => {
        const input = [
            '5) Section',
            '  - Item A',
            '    - Item B'
        ].join('\n');

        expect(detectMarkdown(input)).toBe(true);
    });

    test('removes orphan bullet-only line before fenced code block', () => {
        const input = [
            'Once you confirm, I will immediately perform:',
            '',
            '•',
            '',
            '```',
            'add_relationship',
            'source: #V#gpt-4o',
            '```',
            '',
        ].join('\n');

        const html = simpleMarkdownToHtml(input);

        // No stray bullet rendered.
        expect(html).not.toContain('•');
        // Code block still present.
        expect(html).toContain('<pre><code>');
        expect(html).toContain('add_relationship');
    });

    test('removes orphan dash bullet-only line before fenced code block', () => {
        const input = [
            'Do this:',
            '',
            '-',
            '',
            '```json',
            '{"a": 1}',
            '```',
        ].join('\n');

        const html = simpleMarkdownToHtml(input);
        expect(html).not.toContain('<br>-<br>');
        expect(html).toContain('<pre><code>');
        expect(html).toContain('{&quot;a&quot;: 1}');
    });

    test('renders horizontal rules from --- markers', () => {
        const input = [
            'Before',
            '',
            '---',
            '',
            'After'
        ].join('\n');

        const html = simpleMarkdownToHtml(input);
        expect(html).toContain('<hr>');
        expect(html).toContain('<p>Before</p>');
        expect(html).toContain('<p>After</p>');
    });

    test('renders markdown tables into table html', () => {
        const input = [
            '| Term | Description |',
            '|---|---|',
            '| SGS | School of Graduate Studies |',
            '| BoGS | Board of Graduate Studies |'
        ].join('\n');

        const html = simpleMarkdownToHtml(input);
        expect(html).toContain('<table>');
        expect(html).toContain('<thead>');
        expect(html).toContain('<tbody>');
        expect(html).toContain('<th>Term</th>');
        expect(html).toContain('<td>School of Graduate Studies</td>');
    });

    test('does not inject line-break tags between list items', () => {
        const input = [
            '- Item one',
            '- Item two'
        ].join('\n');

        const html = simpleMarkdownToHtml(input);
        expect(html).toContain('<ul><li>Item one</li><li>Item two</li></ul>');
        expect(html).not.toContain('</li><br><li>');
    });
});

describe('shared conversation links', () => {
    const bare = 'https://github.com/Strong-AI-Lab/knowkat/pull/5';
    const labelled = 'https://github.com/Strong-AI-Lab/Von-Private/pull/67';
    const parse = html => { const el = document.createElement('div'); el.innerHTML = html; return el; };
    test('detects bare links and preserves destinations and trailing punctuation', () => {
        expect(detectMarkdown(`Published ${bare}.`)).toBe(true);
        const el = parse(simpleMarkdownToHtml(`Published (${bare}), [PR](${labelled}). https://example.org/a(b)?x=1&y=2!`));
        expect([...el.querySelectorAll('a')].map(a => a.getAttribute('href'))).toEqual([bare, labelled, 'https://example.org/a(b)?x=1&y=2']);
        expect(el.textContent).toBe(`Published (${bare}), PR. https://example.org/a(b)?x=1&y=2!`);
        expect([...el.querySelectorAll('a')].every(a => a.rel === 'noopener noreferrer' && a.target === '_blank')).toBe(true);
    });
    test('keeps code literal, rejects executable schemes and escapes injected markup', () => {
        const el = parse(simpleMarkdownToHtml('`[literal](https://example.org) https://example.org`\n\n```\nhttps://example.org\n```\n\n[bad](javascript:alert) [bad](data:text/html,x) [bad](vbscript:x) <img src=x onerror=alert(1)>'));
        expect(el.querySelectorAll('a, img, script')).toHaveLength(0);
        expect(el.querySelector('code').textContent).toBe('[literal](https://example.org) https://example.org');
    });
    test('labelled query strings are escaped once and existing concept links survive', () => {
        const el = parse(simpleMarkdownToHtml('[query](https://example.org/?a=1&b=2) [task](#V#task_example)'));
        expect(el.querySelector('a').getAttribute('href')).toBe('https://example.org/?a=1&b=2');
        expect(el.querySelectorAll('a')[1].getAttribute('href')).toBe('#V#task_example');
    });
    test('server-rendered text is linkified without nesting anchors or touching code', async () => {
        const { renderMarkdownViaServer } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');
        global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ html: `<p>${bare}. <a href="${labelled}">PR</a> <code>${bare}</code></p>` }) });
        const el = parse(await renderMarkdownViaServer(bare));
        expect([...el.querySelectorAll('a')].map(a => a.getAttribute('href'))).toEqual([bare, labelled]);
        expect(el.querySelector('code a')).toBeNull();
        delete global.fetch;
    });
});
