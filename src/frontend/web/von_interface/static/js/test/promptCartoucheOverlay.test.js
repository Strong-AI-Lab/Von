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

    test('re-syncs overlay scroll translation when overlay becomes taller than textarea scroll range', () => {
        const textarea = document.querySelector('#promptInput');

        // Simulate a scrollable textarea.
        Object.defineProperty(textarea, 'clientHeight', { value: 200, configurable: true });
        Object.defineProperty(textarea, 'scrollHeight', { value: 1000, configurable: true });
        textarea.scrollTop = 800; // max scrollTop given values above

        initializePromptCartoucheOverlay(textarea);

        const wrapper = textarea.parentElement;
        const inner = wrapper.querySelector('.prompt-input-overlay-inner');
        expect(inner).not.toBeNull();

        // Simulate the overlay content being taller (e.g., after cartouche hydration changes wrapping).
        // overlayMaxScroll = 1400 - 200 = 1200 vs textareaMaxScroll = 1000 - 200 = 800
        Object.defineProperty(inner, 'scrollHeight', { value: 1400, configurable: true });

        // Force the queued scroll sync to run.
        jest.runAllTimers();

        // With mapping: effectiveScrollTop = 800/800 * 1200 = 1200
        expect(inner.style.transform).toBe('translateY(-1200px)');
    });
});
