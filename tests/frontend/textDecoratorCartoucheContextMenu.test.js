/** @jest-environment jsdom */

const modulePath = '../../src/frontend/web/von_interface/static/js/utils/textDecorator.js';

describe('Vontology cartouche context menu', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="root"></div>';
        // Clipboard API mock
        Object.defineProperty(global.navigator, 'clipboard', {
            value: { writeText: jest.fn().mockResolvedValue(undefined) },
            configurable: true
        });
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('right-click shows Copy concept ID action', async () => {
        const { createVontologyCartouche } = require(modulePath);

        const cartouche = createVontologyCartouche('person', { name: 'Person', kind: 'type' });
        document.getElementById('root').appendChild(cartouche);

        cartouche.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 10, clientY: 10 }));

        const menu = document.querySelector('.cartouche-context-menu');
        expect(menu).not.toBeNull();
        expect(menu.style.display).toBe('block');

        const item = menu.querySelector('button.cartouche-context-menu-item');
        expect(item).not.toBeNull();

        item.click();
        // Allow async clipboard write to resolve.
        await new Promise(r => setTimeout(r, 0));

        expect(global.navigator.clipboard.writeText).toHaveBeenCalledWith('#V#person');
    });

    test('click dispatches von:selectConceptById with kind + modifierKeys', () => {
        const { createVontologyCartouche } = require(modulePath);

        const cartouche = createVontologyCartouche('ai_researcher', { name: 'AI Researcher', kind: 'type' });
        document.getElementById('root').appendChild(cartouche);

        const seen = [];
        document.addEventListener('von:selectConceptById', (e) => { seen.push(e.detail); });

        cartouche.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(seen.length).toBe(1);
        expect(seen[0].conceptId).toBe('ai_researcher');
        expect(seen[0].createConceptTab).toBe(true);
        expect(seen[0].kind).toBe('type');
        expect(seen[0].modifierKeys.shiftKey).toBe(true);
    });

    test('compact kind-background mode hides id/kind pill and keeps cartouche clickable', () => {
        const { createVontologyCartouche } = require(modulePath);

        const cartouche = createVontologyCartouche('#V#panelist_in_event', {
            name: 'Panelist in event',
            kind: 'predicate',
            mode: 'compact_kind_bg'
        });

        expect(cartouche.dataset.cartoucheMode).toBe('compact_kind_bg');
        expect(cartouche.classList.contains('vontology-cartouche-compact-kind-bg')).toBe(true);
        expect(cartouche.classList.contains('cartouche-kind-as-bg')).toBe(true);
        expect(cartouche.classList.contains('cartouche-hide-kind')).toBe(true);
        expect(cartouche.classList.contains('cartouche-hide-id')).toBe(true);
    });
});
