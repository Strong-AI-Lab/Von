/** @jest-environment jsdom */
const fs = require('fs');
const template = fs.readFileSync('src/frontend/web/von_interface/templates/von_interface.html', 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
let moduleUnderTest;
beforeEach(() => {
    jest.resetModules();
    document.body.innerHTML = '<input id="vontologySearchInput"><div id="vontologySearchResults"></div>'
        + template.match(/<details id="searchContentTypes"[\s\S]*?<\/details>/)[0];
    const { elements } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');
    elements.vontologySearchInput = document.querySelector('#vontologySearchInput');
    elements.vontologySearchResults = document.querySelector('#vontologySearchResults');
    global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ results: [], tasks: [], conversations: [] }) }));
    moduleUnderTest = require('../../src/frontend/web/von_interface/static/js/vontology.js');
    moduleUnderTest.setupVontologySearchUI();
});
test('defaults to all; deselected types are not requested or rendered; none is explicit; reselect restores', async () => {
    const input = document.querySelector('#vontologySearchInput');
    input.value = 'research';
    await moduleUnderTest.performVontologySearch(input.value);
    expect(document.querySelectorAll('.unified-search-group')).toHaveLength(3);
    document.querySelector('[value="conversations"]').click();
    document.querySelector('[value="tasks"]').click();
    await tick();
    global.fetch.mockClear();
    await moduleUnderTest.performVontologySearch(input.value);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(String(global.fetch.mock.calls[0][0])).toContain('/vontology/search');
    expect(document.querySelectorAll('.unified-search-group')).toHaveLength(1);
    expect(document.querySelector('#searchContentTypes').classList.contains('is-filtered')).toBe(true);
    document.querySelector('[value="concepts"]').click();
    await tick();
    expect(document.querySelector('#vontologySearchResults').textContent).toContain('Select at least one content type');
    expect(moduleUnderTest.__test_getUnifiedSearchState().items).toHaveLength(0);
    document.querySelectorAll('#searchContentTypes input').forEach(el => el.click());
    await tick();
    expect(document.querySelector('#searchContentTypesCount').textContent).toBe('All');
    expect(document.querySelector('#searchContentTypes').classList.contains('is-filtered')).toBe(false);
    expect(document.querySelectorAll('.unified-search-group')).toHaveLength(3);
});
test('late responses cannot restore a deselected provider; Escape closes and returns focus', async () => {
    let resolveConcept;
    global.fetch.mockImplementation(url => String(url).includes('/vontology/search')
        ? new Promise(resolve => { resolveConcept = resolve; })
        : Promise.resolve({ ok: true, json: async () => ({ results: [], tasks: [], conversations: [] }) }));
    document.querySelector('#vontologySearchInput').value = 'research';
    const pending = moduleUnderTest.performVontologySearch('research');
    await tick();
    document.querySelector('[value="concepts"]').click();
    resolveConcept({ ok: true, json: async () => ({ results: [{ id: '#V#late', name: 'Late concept' }] }) });
    await pending;
    expect(document.querySelector('.unified-search-group-concepts')).toBeNull();
    const details = document.querySelector('details');
    details.open = true;
    details.querySelector('input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(details.open).toBe(false);
    expect(document.activeElement).toBe(details.querySelector('summary'));
});
