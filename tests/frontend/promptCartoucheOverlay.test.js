/** @jest-environment jsdom */

const {
    initializePromptCartoucheOverlay,
    makeNonTriggerVontologyId,
    normaliseVontologyIdsForBackend,
} = require('../../src/frontend/web/von_interface/static/js/components/promptCartoucheOverlay.js');

describe('promptCartoucheOverlay', () => {
    let textarea;

    beforeEach(() => {
        document.body.innerHTML = '';
        textarea = document.createElement('textarea');
        textarea.className = 'prompt-input';
        textarea.placeholder = 'Talk with Von here...';
        document.body.appendChild(textarea);
    });

    test('normaliseVontologyIdsForBackend converts non-trigger prefix back to #V#', () => {
        const raw = `Hello ${makeNonTriggerVontologyId('#V#person')} world`;
        expect(normaliseVontologyIdsForBackend(raw)).toBe('Hello #V#person world');
    });

    test('overlay renders cartouche and remove deletes token from textarea', () => {
        initializePromptCartoucheOverlay(textarea);

        textarea.value = `Hello ${makeNonTriggerVontologyId('#V#person')} world`;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));

        const wrapper = textarea.closest('.prompt-input-wrapper');
        expect(wrapper).toBeTruthy();

        const cartouche = wrapper.querySelector('button.prompt-vontology-cartouche');
        expect(cartouche).toBeTruthy();
        expect(cartouche.dataset.fullConceptId).toBe('#V#person');

        const remove = cartouche.querySelector('.prompt-vontology-cartouche-remove');
        expect(remove).toBeTruthy();
        remove.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(textarea.value).toBe('Hello world');
    });
});
