import {
    __testOnly_disconnectCopyJsonButtonObserver,
    initialiseCopyJsonButtonPreCopyState,
    resetCopyJsonButtonPreCopyState
} from '../utils/copyJsonButtonState.js';

describe('copyJsonButtonState', () => {
    beforeEach(() => {
        __testOnly_disconnectCopyJsonButtonObserver();
        document.body.innerHTML = '';
    });

    afterEach(() => {
        __testOnly_disconnectCopyJsonButtonObserver();
        document.body.innerHTML = '';
    });

    test('marks Copy JSON buttons as pending before first click', () => {
        document.body.innerHTML = '<button id="copy">Copy JSON</button>';

        initialiseCopyJsonButtonPreCopyState();

        const button = document.getElementById('copy');
        expect(button.classList.contains('copy-json-precopy')).toBe(true);
        expect(button.getAttribute('data-copy-json-state')).toBe('pending');
    });

    test('clears pending state after first click', () => {
        document.body.innerHTML = '<button id="copy">Copy JSON</button>';

        initialiseCopyJsonButtonPreCopyState();

        const button = document.getElementById('copy');
        button.click();
        expect(button.classList.contains('copy-json-precopy')).toBe(false);
        expect(button.getAttribute('data-copy-json-state')).toBe('used');
    });

    test('can reset button back to pending for a newly-opened JSON view', () => {
        document.body.innerHTML = '<button id="copy">Copy JSON</button>';

        initialiseCopyJsonButtonPreCopyState();

        const button = document.getElementById('copy');
        button.click();
        resetCopyJsonButtonPreCopyState(button);

        expect(button.classList.contains('copy-json-precopy')).toBe(true);
        expect(button.getAttribute('data-copy-json-state')).toBe('pending');
    });

    test('tracks dynamically-added Copy JSON buttons', async () => {
        initialiseCopyJsonButtonPreCopyState();

        const button = document.createElement('button');
        button.id = 'dynamic-copy';
        button.textContent = 'Copy JSON';
        document.body.appendChild(button);
        await Promise.resolve();

        expect(button.classList.contains('copy-json-precopy')).toBe(true);
    });
});
