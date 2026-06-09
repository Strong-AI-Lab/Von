/**
 * @jest-environment jsdom
 */

describe('Global Vontology search selection', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '';
        global.fetch = undefined;
    });

    test('selectSearchItem dispatches open-concept-tab immediately', async () => {
        // We require the module directly so we can call the exported helper.
        const vontology = require('../../src/frontend/web/von_interface/static/js/vontology.js');
        const { elements } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');

        // Minimal DOM elements expected by selectSearchItem.
        const input = document.createElement('input');
        input.id = 'vontology-search-input';
        document.body.appendChild(input);

        // Initialise the shared elements map used by the module.
        elements.vontologySearchInput = input;

        const events = [];
        document.addEventListener('open-concept-tab', (e) => events.push(e.detail));

        // Arrange: Make fetch hang so any awaited work would stall forever.
        global.fetch = jest.fn(() => new Promise(() => { }));

        // Act: trigger selection.
        const promise = vontology.__test_selectSearchItem({ id: '#V#person', name: 'Person', kind: 'individual' });

        // Assert: event must be dispatched synchronously before any awaits.
        expect(events).toHaveLength(1);
        expect(events[0]).toMatchObject({ conceptId: '#V#person', kind: 'individual' });

        // Clean up: allow any pending promise to settle if it can.
        void promise;
    });

    test('performVontologySearch renders exact-id fallback 404 as visible status row', async () => {
        const vontology = require('../../src/frontend/web/von_interface/static/js/vontology.js');
        const { elements } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');

        const results = document.createElement('div');
        results.id = 'vontologySearchResults';
        document.body.appendChild(results);
        elements.vontologySearchResults = results;
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#michael_witbrock' }));
        sessionStorage.setItem('von_window_session_id', 'ws-search-test');

        global.fetch = jest
            .fn()
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({ results: [] }),
            })
            .mockResolvedValueOnce({
                ok: false,
                status: 404,
                json: async () => ({ error: "Concept '#V#missing' not found in MongoDB." }),
            });

        await vontology.performVontologySearch('#V#missing');

        const row = results.querySelector('.vontology-search-item-error');
        expect(row).not.toBeNull();
        expect(row.getAttribute('aria-disabled')).toBe('true');
        expect(row.textContent).toContain('No accessible concept found for #V#missing');
        expect(results.classList.contains('open')).toBe(true);
        expect(global.fetch.mock.calls[0][1].headers).toMatchObject({
            'X-User-Concept-ID': '#V#michael_witbrock',
            'X-Von-Window-Session': 'ws-search-test'
        });
    });
});
