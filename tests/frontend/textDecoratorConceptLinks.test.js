/** @jest-environment jsdom */
const { simpleMarkdownToHtml, renderMarkdownViaServer } = require('../../src/frontend/web/von_interface/static/js/markdownUtils.js');
const { cartouchifyVontologyTokensInElement, linkifyVontologyTokensInElement } = require('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js');

const ids = ['#V#task_agent_65d772a220b888b99f69da12723eafab', '#V#masataro_asai'];

beforeEach(() => { document.body.innerHTML = '<div id="root"></div>'; });
afterEach(() => { delete global.fetch; });

test.each(ids)('Markdown concept URL navigates with the exact identity %s', (id) => {
    const root = document.getElementById('root');
    root.innerHTML = simpleMarkdownToHtml(`[Readable label](https://von.curiouscat.cc/von/${id})`);
    cartouchifyVontologyTokensInElement(root);
    const button = root.querySelector('button');
    expect(button.dataset.fullConceptId).toBe(id);
    const selected = jest.fn();
    root.addEventListener('von:selectConceptById', selected);
    button.click();
    expect(selected.mock.calls[0][0].detail).toMatchObject({ conceptId: id.slice(3), createConceptTab: true });
    expect(root.querySelector('a')).toBeNull();
});

test('server-rendered anchors, bare URLs, relative URLs and bare IDs share annotation', async () => {
    const root = document.getElementById('root');
    global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ html: `<p><a href="/von/#${encodeURIComponent(ids[0].slice(1))}">Task</a> ${ids[1]}</p>` }) }));
    root.innerHTML = await renderMarkdownViaServer('server fixture');
    cartouchifyVontologyTokensInElement(root);
    expect([...root.querySelectorAll('button')].map(el => el.dataset.fullConceptId)).toEqual(ids);
    root.innerHTML = simpleMarkdownToHtml(`https://von.curiouscat.cc/von/${ids[1]}`);
    linkifyVontologyTokensInElement(root);
    const selected = jest.fn();
    root.addEventListener('von:selectConceptById', selected);
    root.querySelector('a').click();
    expect(selected.mock.calls[0][0].detail.conceptId).toBe(ids[1].slice(3));
});

test('external sources, unrelated routes, malformed fragments and literal code stay untouched', () => {
    const root = document.getElementById('root');
    const hrefs = [
        'https://example.com/paper',
        `https://example.com/von/${ids[1]}`,
        `https://von.curiouscat.cc.example.com/von/${ids[1]}`,
        `https://von.curiouscat.cc/source/${ids[1]}`,
        `https://von.curiouscat.cc/von/?task=other${ids[1]}`,
        'https://von.curiouscat.cc/von/#V#bad%ZZ',
        'https://von.curiouscat.cc/von/#V#bad%20id'
    ];
    root.innerHTML = simpleMarkdownToHtml(hrefs.map(href => `[Source](${href})`).join('\n') + `\n\n\`https://von.curiouscat.cc/von/${ids[1]}\``);
    const before = root.innerHTML;
    cartouchifyVontologyTokensInElement(root);
    expect(root.innerHTML).toBe(before);
    expect([...root.querySelectorAll('a')].map(el => el.getAttribute('href'))).toEqual(hrefs);
    expect([...root.querySelectorAll('a')].every(el => el.target === '_blank')).toBe(true);
});

test('caller-excluded content is not normalised', () => {
    const root = document.getElementById('root');
    root.innerHTML = `<div class="conversation-visual"><a href="https://von.curiouscat.cc/von/${ids[1]}">Literal diagram link</a></div>`;
    const before = root.innerHTML;
    cartouchifyVontologyTokensInElement(root, {skipSelectors: ['a', '.conversation-visual']});
    expect(root.innerHTML).toBe(before);
});
