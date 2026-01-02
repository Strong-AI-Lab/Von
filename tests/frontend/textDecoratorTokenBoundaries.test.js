/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/textDecorator.js';

describe('Vontology token boundaries', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="root"></div>';
    });

    test('cartouchifies #V# tokens followed by punctuation', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'See #V#person.';

        cartouchifyVontologyTokensInElement(root);

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#person');

        const buttonNode = cartouches[0];
        expect(buttonNode.nextSibling).not.toBeNull();
        expect(buttonNode.nextSibling.nodeType).toBe(Node.TEXT_NODE);
        expect(buttonNode.nextSibling.nodeValue).toBe('.');
    });

    test('cartouchifies #V# tokens inside parentheses at sentence boundaries', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.textContent = 'See (#V#person).';

        cartouchifyVontologyTokensInElement(root);

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#person');

        // Ensure trailing punctuation is preserved as text (" )." after the cartouche).
        const buttonNode = cartouches[0];
        expect(buttonNode.nextSibling).not.toBeNull();
        expect(buttonNode.nextSibling.nodeType).toBe(Node.TEXT_NODE);
        expect(buttonNode.nextSibling.nodeValue).toBe(').');
    });

    test('cartouchifies standalone #V# token in code blocks with em dash', () => {
        const { cartouchifyVontologyTokensInElement } = require(modulePath);

        const root = document.getElementById('root');
        root.innerHTML = '<pre><code>#V#mjw_todo_list_—_2026-01-03</code></pre>';

        cartouchifyVontologyTokensInElement(root, {
            skipSelectors: ['pre', 'code', 'a'],
            allowStandaloneCodeBlockTokens: true
        });

        const cartouches = root.querySelectorAll('.vontology-cartouche');
        expect(cartouches.length).toBe(1);
        expect(cartouches[0].dataset.fullConceptId).toBe('#V#mjw_todo_list_—_2026-01-03');
    });
});
