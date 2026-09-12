/** @jest-environment jsdom */
const { protectMathSources } = require('../../src/frontend/web/von_interface/static/js/conversationVisuals.js');
const { simpleMarkdownToHtml } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');

test('math delimiters preserve exact source and exclude currency, escapes and code', () => {
    const untouched = String.raw`$25 and \$30, \\(escaped\\), ` + '`\\(code\\)`';
    const fence = '\n```tex\n\\[x_i\\]\n```\n';
    const math = String.raw`\(p(x\mid y)\) and \[\alpha_i^2=\frac{1}{2}\]`;
    const protectedText = protectMathSources(untouched + fence + math);
    expect(protectedText.text).toContain(untouched + fence);
    const holder = document.createElement('div');
    holder.innerHTML = protectedText.restore(simpleMarkdownToHtml(protectedText.text));
    expect([...holder.querySelectorAll('.von-math-source')].map(e => e.textContent))
        .toEqual([String.raw`\(p(x\mid y)\)`, String.raw`\[\alpha_i^2=\frac{1}{2}\]`]);
});

test('incomplete source can complete on a later render without sticky parse state', () => {
    const incomplete = String.raw`Before \(p(x`;
    expect(protectMathSources(incomplete).text).toBe(incomplete);
    expect(protectMathSources(incomplete + String.raw`\mid y)\)`).text).not.toContain(String.raw`\(p`);
});

test.each([true, false])('math source and bare links coexist with server available=%s', async (serverAvailable) => {
    const { renderMarkdownViaServer } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');
    const previousFetch = global.fetch;
    const warning = jest.spyOn(console, 'warn').mockImplementation(() => {});
    global.fetch = jest.fn(async (_url, options) => ({
        ok: serverAvailable,
        status: serverAvailable ? 200 : 503,
        json: async () => ({ html: `<p>${JSON.parse(options.body).text}</p>` })
    }));
    try {
        const maths = String.raw`\(\text{https://example.org/formula}\)`;
        const holder = document.createElement('div');
        holder.innerHTML = await renderMarkdownViaServer(`${maths} See https://example.org/paper.`);
        expect(holder.querySelector('.von-math-source').textContent).toBe(maths);
        expect(holder.querySelectorAll('a')).toHaveLength(1);
        expect(holder.querySelector('a').getAttribute('href')).toBe('https://example.org/paper');
        expect(holder.textContent).toContain('See https://example.org/paper.');
    } finally {
        global.fetch = previousFetch;
        warning.mockRestore();
    }
});

test('fallback Mermaid fence keeps exact body and cannot inject HTML', () => {
    const source = 'flowchart LR\n A["<script>attack()</script>"] --> B[Safe]\n';
    const holder = document.createElement('div');
    holder.innerHTML = simpleMarkdownToHtml('```mermaid\n' + source + '```');
    expect(holder.querySelector('code.language-mermaid').textContent).toBe(source);
    expect(holder.querySelector('script')).toBeNull();
});

test('an unavailable original has a local notice and duplicate events keep one image', () => {
    const { renderImageAttachments } = require('../../src/frontend/web/von_interface/static/js/utils/conversationImages.js');
    const holder = document.createElement('div');
    const image = { concept_id: '#V#expired-image', provenance: { kind: 'generated' } };
    renderImageAttachments(holder, [image]);
    renderImageAttachments(holder, [image]);
    holder.querySelector('img').dispatchEvent(new Event('error'));
    holder.querySelector('img').dispatchEvent(new Event('error'));
    expect(holder.querySelectorAll('img')).toHaveLength(1);
    expect(holder.querySelectorAll('.image-unavailable')).toHaveLength(1);
    expect(holder.textContent).toContain('Image unavailable or access denied');
    expect(holder.querySelector('a').getAttribute('href')).toBe('/von/api/images/%23V%23expired-image/original');
});
