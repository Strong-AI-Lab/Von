/**
 * @jest-environment jsdom
 */

describe('Global Vontology search selection', () => {
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
});
