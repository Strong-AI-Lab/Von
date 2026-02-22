import {
    copyJsonTextWithButtonFeedback,
    __testOnly_disconnectCopyJsonButtonObserver,
    indicateCopyJsonButtonResult,
    initialiseCopyJsonButtonPreCopyState,
    resetCopyJsonButtonPreCopyState
} from '../utils/copyJsonButtonState.js';

describe('copyJsonButtonState', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        __testOnly_disconnectCopyJsonButtonObserver();
        document.body.innerHTML = '';
    });

    afterEach(() => {
        jest.useRealTimers();
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

    test('shows copied state with checkmark then restores original label', () => {
        document.body.innerHTML = '<button id="copy" title="Copy JSON">Copy JSON</button>';
        const button = document.getElementById('copy');

        indicateCopyJsonButtonResult(button, true);

        expect(button.textContent).toBe('✓ Copied');
        expect(button.classList.contains('copy-json-copied')).toBe(true);
        expect(button.getAttribute('data-copy-json-state')).toBe('used');

        jest.advanceTimersByTime(2300);
        expect(button.textContent).toBe('Copy JSON');
        expect(button.classList.contains('copy-json-copied')).toBe(false);
    });

    test('copyJsonTextWithButtonFeedback applies failure state when clipboard write fails', async () => {
        document.body.innerHTML = '<button id="copy">Copy JSON</button>';
        const button = document.getElementById('copy');
        const writeText = jest.fn().mockRejectedValue(new Error('denied'));
        const execCommand = jest.fn(() => false);
        Object.assign(navigator, {
            clipboard: { writeText }
        });
        document.execCommand = execCommand;

        const copied = await copyJsonTextWithButtonFeedback(button, '{"ok":true}');
        expect(copied).toBe(false);
        expect(button.textContent).toBe('Copy failed');
        expect(button.classList.contains('copy-json-copy-failed')).toBe(true);
    });
});
