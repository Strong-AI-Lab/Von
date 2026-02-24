/** @jest-environment jsdom */

const { detectMarkdown, simpleMarkdownToHtml } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');

describe('markdownUtils', () => {
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
