const {
    initialiseCompactChatComposer,
    shouldSubmitComposerKey
} = require('../../src/frontend/web/von_interface/static/js/components/compactComposer.js');

let menu, summary, input, send;
const flushToggle = () => menu.dispatchEvent(new Event('toggle'));

beforeEach(() => {
    window.matchMedia = jest.fn(() => ({ matches: true, addEventListener: jest.fn() }));
    document.body.innerHTML = `<div class="chat-composer">
        <textarea id="promptInput">Preserved draft</textarea><button id="sendButton">Send</button>
        <details class="chat-composer-more-actions"><summary><span class="composer-more-label">More actions</span></summary>
        <div class="button-row"><button id="uploadFileButton">Upload</button><select><option>Engine</option></select></div></details>
        </div><button id="outside">Outside</button>`;
    menu = document.querySelector('details');
    summary = document.querySelector('summary');
    input = document.querySelector('textarea');
    send = document.querySelector('#sendButton');
    initialiseCompactChatComposer(document.querySelector('.chat-composer'), () => {});
});

afterEach(() => {
    menu.open = false;
    flushToggle();
    delete window.CloseWatcher;
    document.body.innerHTML = '';
});

function open() {
    menu.open = true;
    flushToggle();
}

test('outside click dismisses without clearing the draft; option interactions remain open', () => {
    open();
    document.querySelector('select').click();
    expect(menu.open).toBe(true);
    document.querySelector('#outside').click();
    expect(menu.open).toBe(false);
    expect(input.value).toBe('Preserved draft');
});

test('Send reaches its existing handler exactly once with the draft intact and options closed', () => {
    const submit = jest.fn(() => {
        expect(menu.open).toBe(false);
        expect(input.value).toBe('Preserved draft');
    });
    send.addEventListener('click', submit);
    open();
    send.click();
    expect(submit).toHaveBeenCalledTimes(1);
});

test('Escape restores toggle focus; leaving the options preserves destination focus', () => {
    open();
    const option = document.querySelector('select');
    option.focus();
    option.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(summary);
    open();
    option.focus();
    input.focus();
    expect(menu.open).toBe(true);
    input.click();
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(input);
});

test('browser close requests close the menu, restore focus and release the watcher', () => {
    const watchers = [];
    window.CloseWatcher = class extends EventTarget {
        constructor() { super(); this.destroy = jest.fn(); watchers.push(this); }
    };
    open();
    expect(watchers).toHaveLength(1);
    watchers[0].dispatchEvent(new Event('close'));
    expect(menu.open).toBe(false);
    expect(document.activeElement).toBe(summary);
    expect(watchers[0].destroy).toHaveBeenCalledTimes(1);
    open();
    expect(watchers).toHaveLength(2);
    summary.click();
    flushToggle();
    expect(watchers[1].destroy).toHaveBeenCalledTimes(1);
});

test('desktop outside clicks keep existing disclosure behaviour', () => {
    window.matchMedia.mockReturnValue({ matches: false });
    open();
    document.querySelector('#outside').click();
    expect(menu.open).toBe(true);
});

test('mobile Enter remains a newline; desktop submission and composition rules are preserved', () => {
    expect(shouldSubmitComposerKey({ key: 'Enter' })).toBe(false);
    window.matchMedia.mockReturnValue({ matches: false });
    expect(shouldSubmitComposerKey({ key: 'Enter' })).toBe(true);
    expect(shouldSubmitComposerKey({ key: 'Enter', shiftKey: true })).toBe(false);
    expect(shouldSubmitComposerKey({ key: 'Enter', isComposing: true })).toBe(false);
    expect(shouldSubmitComposerKey({ key: 'Enter', keyCode: 229 })).toBe(false);
});
