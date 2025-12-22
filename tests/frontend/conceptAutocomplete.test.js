/** @jest-environment jsdom */

/**
 * Tests for Concept Autocomplete Component
 */

const conceptAutocompleteModule = require('../../src/frontend/web/von_interface/static/js/components/conceptAutocomplete.js');
const { closeAutocomplete, initializeConceptAutocomplete } = conceptAutocompleteModule;

describe('conceptAutocomplete', () => {
    let textarea;
    let container;

    beforeEach(() => {
        // Create a mock container and textarea
        container = document.createElement('div');
        container.id = 'test-container';

        textarea = document.createElement('textarea');
        textarea.id = 'testPromptInput';
        textarea.value = '';

        container.appendChild(textarea);
        document.body.appendChild(container);
    });

    afterEach(() => {
        closeAutocomplete();
        document.body.removeChild(container);
    });

    test('initializeConceptAutocomplete sets up event listeners', () => {
        const addEventListenerSpy = jest.spyOn(textarea, 'addEventListener');

        initializeConceptAutocomplete(textarea);

        expect(addEventListenerSpy).toHaveBeenCalledWith('input', expect.any(Function));
        expect(addEventListenerSpy).toHaveBeenCalledWith('keydown', expect.any(Function));
        expect(addEventListenerSpy).toHaveBeenCalledWith('blur', expect.any(Function));

        addEventListenerSpy.mockRestore();
    });

    test('closeAutocomplete hides the dropdown', () => {
        const dropdown = document.createElement('div');
        dropdown.className = 'concept-autocomplete-dropdown';
        dropdown.style.display = 'block';
        document.body.appendChild(dropdown);

        closeAutocomplete();

        expect(dropdown.style.display).toBe('none');

        document.body.removeChild(dropdown);
    });

    test('autocomplete does not trigger without #V# prefix', (done) => {
        initializeConceptAutocomplete(textarea);

        textarea.value = 'Hello world';
        textarea.selectionStart = textarea.value.length;
        textarea.selectionEnd = textarea.value.length;

        const inputEvent = new Event('input', { bubbles: true });
        textarea.dispatchEvent(inputEvent);

        setTimeout(() => {
            const dropdown = document.querySelector('.concept-autocomplete-dropdown');
            expect(dropdown === null || dropdown.style.display === 'none').toBe(true);
            done();
        }, 300);
    });

    test('autocomplete triggers on #V# prefix', (done) => {
        initializeConceptAutocomplete(textarea);

        // Mock fetch to return test results
        global.fetch = jest.fn(() =>
            Promise.resolve({
                ok: true,
                json: () =>
                    Promise.resolve({
                        results: [
                            { id: '#V#person', name: 'Person', kind: 'type' },
                            { id: '#V#thing', name: 'Thing', kind: 'type' },
                        ],
                    }),
            })
        );

        textarea.value = '#V#pe';
        textarea.selectionStart = textarea.value.length;
        textarea.selectionEnd = textarea.value.length;

        const inputEvent = new Event('input', { bubbles: true });
        textarea.dispatchEvent(inputEvent);

        setTimeout(() => {
            const dropdown = document.querySelector('.concept-autocomplete-dropdown');
            const items = dropdown
                ? dropdown.querySelectorAll('.concept-autocomplete-item')
                : [];
            expect(items.length).toBeGreaterThan(0);
            done();
        }, 300);
    });

    test('autocomplete is case-insensitive (#v# works)', (done) => {
        initializeConceptAutocomplete(textarea);

        global.fetch = jest.fn(() =>
            Promise.resolve({
                ok: true,
                json: () =>
                    Promise.resolve({
                        results: [{ id: '#V#person', name: 'Person', kind: 'type' }],
                    }),
            })
        );

        textarea.value = '#v#pe';
        textarea.selectionStart = textarea.value.length;
        textarea.selectionEnd = textarea.value.length;

        const inputEvent = new Event('input', { bubbles: true });
        textarea.dispatchEvent(inputEvent);

        setTimeout(() => {
            const dropdown = document.querySelector('.concept-autocomplete-dropdown');
            const items = dropdown
                ? dropdown.querySelectorAll('.concept-autocomplete-item')
                : [];
            expect(items.length).toBeGreaterThan(0);
            done();
        }, 300);
    });
});
