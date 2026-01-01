import { initializePromptCartoucheOverlay, makeNonTriggerVontologyId } from '../components/promptCartoucheOverlay.js';

describe('prompt cartouche overlay', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = `
            <textarea class="prompt-input" id="promptInput"></textarea>
        `;
    });

    afterEach(() => {
        jest.runOnlyPendingTimers();
        jest.useRealTimers();
    });

    test('wraps textarea and creates overlay, mirror, and caret elements', () => {
        const textarea = document.querySelector('#promptInput');
        initializePromptCartoucheOverlay(textarea);

        const wrapper = textarea.parentElement;
        expect(wrapper).not.toBeNull();
        expect(wrapper.classList.contains('prompt-input-wrapper')).toBe(true);
        expect(wrapper.classList.contains('has-overlay-caret')).toBe(true);

        const overlay = wrapper.querySelector('.prompt-input-overlay');
        expect(overlay).not.toBeNull();
        expect(wrapper.querySelector('.prompt-input-overlay-inner')).not.toBeNull();
        expect(wrapper.querySelector('.prompt-input-overlay-content')).not.toBeNull();
        expect(wrapper.querySelector('.prompt-input-overlay-mirror')).not.toBeNull();
        expect(wrapper.querySelector('.prompt-input-overlay-mirror-content')).not.toBeNull();
        expect(wrapper.querySelector('.prompt-input-overlay-caret')).not.toBeNull();
    });

    test('renders cartouche tokens into overlay content', () => {
        const textarea = document.querySelector('#promptInput');
        initializePromptCartoucheOverlay(textarea);

        textarea.value = `Hello ${makeNonTriggerVontologyId('#V#literary_work')} world`;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));

        const wrapper = textarea.parentElement;
        const content = wrapper.querySelector('.prompt-input-overlay-content');
        expect(content).not.toBeNull();
        const cartouches = content.querySelectorAll('button.prompt-vontology-cartouche');
        expect(cartouches.length).toBe(1);
    });
});
