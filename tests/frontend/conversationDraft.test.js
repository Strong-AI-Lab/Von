/** @jest-environment jsdom */
import { initialiseConversationDraft, navigateConversationDraftHistory, normaliseVontologyIdsForBackend, setConversationDraft } from '../../src/frontend/web/von_interface/static/js/components/conversationDraft.js';
import { closeAutocomplete } from '../../src/frontend/web/von_interface/static/js/components/conceptAutocomplete.js';

const tick = () => new Promise(resolve => setTimeout(resolve, 320));

describe('shared conversation draft', () => {
    let input, send, submit, changed;
    const originalFetch = global.fetch;
    beforeEach(() => {
        document.body.innerHTML = '<textarea id="draft"></textarea><button id="send">Send</button><input id="recipient">';
        input = document.getElementById('draft'); send = document.getElementById('send');
        submit = jest.fn(); changed = jest.fn();
        window.matchMedia = jest.fn(() => ({ matches: false }));
        global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ results: [{ id: '#V#research', name: 'Research', kind: 'type' }] }) }));
        initialiseConversationDraft({ input, sendButton: send, onSubmit: submit, onInput: changed });
    });
    afterEach(() => { closeAutocomplete(); global.fetch = originalFetch; });

    test('concept selection consumes Enter before submission and normalises for delivery', async () => {
        input.focus(); input.value = '#V#res'; input.setSelectionRange(6, 6);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        await tick();
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', cancelable: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
        expect(submit).not.toHaveBeenCalled();
        expect(normaliseVontologyIdsForBackend(input.value).trim()).toBe('#V#research');
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
        expect(submit).toHaveBeenCalledTimes(1);
    });

    test('newly focused draft search survives the previous field blur timer', async () => {
        const { initializeConceptAutocomplete } = require('../../src/frontend/web/von_interface/static/js/components/conceptAutocomplete.js');
        const recipient = document.getElementById('recipient');
        initializeConceptAutocomplete(recipient);
        recipient.focus(); input.focus();
        input.value = '#V#res'; input.setSelectionRange(6, 6);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        await tick();
        expect(document.querySelector('.concept-autocomplete-dropdown').style.display).not.toBe('none');
    });

    test('restoring draft updates the editor without invalidating delivery recovery', () => {
        setConversationDraft(input, 'Retained draft');
        expect(input.value).toBe('Retained draft');
        expect(input.dataset.hasDraft).toBe('true');
        expect(changed).not.toHaveBeenCalled();
    });

    test('mobile, composition and Shift+Enter never submit', () => {
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, cancelable: true }));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, cancelable: true }));
        window.matchMedia.mockReturnValue({ matches: true });
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
        expect(submit).not.toHaveBeenCalled();
        send.click(); expect(submit).toHaveBeenCalledTimes(1);
    });

    test('history recall preserves edits and returns to an empty draft', () => {
        const event = key => new KeyboardEvent('keydown', { key, cancelable: true });
        expect(navigateConversationDraftHistory(event('ArrowUp'), input, ['First', 'Last'], 2)).toEqual({ cursor: 1, value: 'Last' });
        input.value = 'Last';
        expect(navigateConversationDraftHistory(event('ArrowDown'), input, ['First', 'Last'], 1)).toEqual({ cursor: 2, value: '' });
        input.value = 'My edit';
        expect(navigateConversationDraftHistory(event('ArrowUp'), input, ['First', 'Last'], 1)).toBeNull();
    });
});
