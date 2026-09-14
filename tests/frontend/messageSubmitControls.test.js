/** @jest-environment jsdom */
import { createMessageSubmitControls } from '../../src/frontend/web/von_interface/static/js/components/messageSubmitControls.js';
import { createChatSteeringControls } from '../../src/frontend/web/von_interface/static/js/components/chatSteeringControls.js';
import { getSubmitMode, setSubmitMode } from '../../src/frontend/web/von_interface/static/js/components/submitMode.js';

let controls;
let actor;
let submit;
let unavailable;
let button;
beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML = '<div><div><button id="submit"></button></div></div>';
    actor = 'alice'; submit = jest.fn(); unavailable = jest.fn();
    button = document.getElementById('submit');
    controls = createMessageSubmitControls({ button, getActor: () => actor, onSubmit: submit, onUnavailable: unavailable });
});
afterEach(() => controls.dispose());

test('arrow is icon-only with accessible mode; Shift is one-time and never sends unsupported steering', () => {
    expect(button.querySelector('svg')).not.toBeNull();
    expect(button.getAttribute('aria-label')).toBe('Submit separate message');
    controls.activate({ shiftKey: true });
    expect(submit).not.toHaveBeenCalled();
    expect(unavailable).toHaveBeenCalledWith(expect.stringContaining('Draft and attachments retained'));
    expect(getSubmitMode(actor)).toBe('queue');
    controls.activate();
    expect(submit).toHaveBeenCalledTimes(1);
});

test('saved preference is shared live with normal chat; alternate action and actor change remain independent', () => {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = '<button id="chat">Submit</button>';
    document.body.append(wrapper);
    const chat = createChatSteeringControls({ sendButton: wrapper.firstChild, request: async () => ({ items: [] }), getDraft: () => '', clearDraft() {}, createId: () => 'fixture' });
    try {
        chat.update({ actorKey: actor, scopeKey: actor, busy: false });
        const chatSelect = document.getElementById('chatSubmitMode');
        chatSelect.value = 'steer'; chatSelect.dispatchEvent(new Event('change'));
        expect(button.dataset.submitMode).toBe('steer');
        expect(button.getAttribute('aria-label')).toContain('unavailable');
        controls.activate();
        expect(submit).not.toHaveBeenCalled();
        controls.activate({ shiftKey: true });
        expect(submit).toHaveBeenCalledTimes(1);
        expect(getSubmitMode(actor)).toBe('steer');
        const selector = document.querySelector('.message-submit-actions select');
        selector.value = 'queue'; selector.dispatchEvent(new Event('change'));
        expect(chatSelect.value).toBe('queue');
        setSubmitMode('alice', 'steer');
        actor = 'bob'; controls.update();
        expect(button.dataset.submitMode).toBe('queue');
    } finally { chat.dispose(); }
});

test('touch/keyboard explicit queue respects pending and keeps the saved default', () => {
    setSubmitMode(actor, 'steer');
    const actions = document.querySelectorAll('.message-submit-actions button');
    actions[0].click();
    expect(submit).toHaveBeenCalledTimes(1);
    expect(getSubmitMode(actor)).toBe('steer');
    button.disabled = true; controls.update(true);
    actions[0].click(); controls.activate({ shiftKey: true });
    expect(submit).toHaveBeenCalledTimes(1);
    expect(button.querySelector('svg')).not.toBeNull();
    expect(button.getAttribute('aria-busy')).toBe('true');
    const details = document.querySelector('details'); details.open = true;
    details.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(details.open).toBe(false);
    expect(document.activeElement).toBe(details.querySelector('summary'));
});
