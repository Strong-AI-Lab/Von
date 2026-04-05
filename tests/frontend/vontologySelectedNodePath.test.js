/** @jest-environment jsdom */

describe('Vontology selected node path cartouches', () => {
    test('renders selected node and parents as clickable references while preserving badges', () => {
        const vontology = require('../../src/frontend/web/von_interface/static/js/vontology.js');

        const target = document.createElement('span');
        target.innerHTML = `
            <span class="forced-visible-badge">forced visible</span>
            <span class="predicate-badge">predicate</span>
        `;
        document.body.appendChild(target);

        const seen = [];
        const onSelect = (event) => seen.push(event.detail);
        document.addEventListener('von:selectConceptById', onSelect);

        try {
            vontology.__test_renderSelectedNodePathContent(
                target,
                { name: 'Selected predicate', conceptId: '#V#selected_predicate' },
                [
                    { name: 'Parent concept', conceptId: '#V#parent_concept' },
                    { name: 'Secondary parent', conceptId: 'secondary_parent' },
                    { name: 'Legacy parent label' }
                ]
            );

            const cartouches = Array.from(target.querySelectorAll('button.vontology-cartouche'));
            expect(cartouches).toHaveLength(3);
            expect(target.textContent).toContain('Parents:');
            expect(target.textContent).toContain('Legacy parent label');
            expect(target.querySelector('.forced-visible-badge')).toBeTruthy();
            expect(target.querySelector('.predicate-badge')).toBeTruthy();

            cartouches[1].dispatchEvent(new MouseEvent('click', { bubbles: true }));
            expect(seen).toHaveLength(1);
            expect(seen[0]).toMatchObject({
                conceptId: 'parent_concept',
                createConceptTab: true
            });
        } finally {
            document.removeEventListener('von:selectConceptById', onSelect);
        }
    });
});
