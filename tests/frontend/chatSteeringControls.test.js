/** @jest-environment jsdom */
import { createChatSteeringControls } from '../../src/frontend/web/von_interface/static/js/components/chatSteeringControls.js';

const tick = async () => { await Promise.resolve(); await Promise.resolve(); };
let control;
let request;
let draft;
let options;
let counter;
beforeEach(() => {
    jest.useFakeTimers();
    localStorage.clear();
    document.body.innerHTML = '<div><button id="send">Queue Prompt</button></div>';
    draft = 'Correct the scope';
    counter = 0;
    request = jest.fn(async () => ({ items: [] }));
    control = createChatSteeringControls({
        sendButton: document.getElementById('send'), request,
        getDraft: () => draft,
        clearDraft: (text) => { if (draft === text) draft = ''; },
        createId: () => `id-${++counter}`
    });
    options = { scopeKey: 'actor:session', target: { queueId: 'queue', attemptId: 'attempt' }, disabled: false };
    control.update(options);
});
afterEach(() => { control.dispose(); jest.useRealTimers(); });

test('Steer targets exact active attempt; pending guidance is cancellable', async () => {
    await tick();
    request.mockImplementation(async (_url, opts) => {
        if (opts.method === 'POST') return { items: [{ id: 'id-1', text: draft, status: 'pending' }] };
        if (opts.method === 'DELETE') return { items: [{ id: 'id-1', text: 'Correct the scope', status: 'cancelled' }] };
        return { items: [{ id: 'id-1', text: 'Correct the scope', status: 'pending' }] };
    });
    document.getElementById('steerButton').click();
    await tick();
    const [, post] = request.mock.calls.find(([, opts]) => opts.method === 'POST');
    expect(JSON.parse(post.body)).toEqual({ text: 'Correct the scope', submission_id: 'id-1', attempt_id: 'attempt' });
    expect(draft).toBe('');
    expect(document.body.textContent).toContain('Pending steer');
    [...document.querySelectorAll('button')].find(b => b.textContent === 'Cancel steer').click();
    await tick();
    expect(document.body.textContent).toContain('Steer cancelled');
});

test('uncertain submission preserves draft and reuses ID on retry', async () => {
    await tick();
    request.mockImplementation(async (_url, opts) => {
        if (opts.method === 'POST') throw new Error('Connection lost');
        return { items: [] };
    });
    document.getElementById('steerButton').click();
    await tick();
    expect(draft).toBe('Correct the scope');
    expect(document.body.textContent).toContain('Draft retained');
    document.getElementById('steerButton').click();
    await tick();
    const posts = request.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(posts).toHaveLength(2);
    expect(posts[0][1].body).toBe(posts[1][1].body);
});

test('session switch isolates late acknowledgements and leaves new draft alone', async () => {
    await tick();
    let resolve;
    request.mockImplementation((_url, opts) => opts.method === 'POST'
        ? new Promise(r => { resolve = r; }) : Promise.resolve({ items: [] }));
    document.getElementById('steerButton').click();
    control.update({ scopeKey: 'actor:other-session', target: null, disabled: false });
    draft = 'New session draft';
    resolve({ items: [{ id: 'old', text: 'Private old text', status: 'delivered' }] });
    await tick();
    expect(draft).toBe('New session draft');
    expect(document.body.textContent).not.toContain('Private old text');
    expect(document.getElementById('steerButton').hidden).toBe(true);
});

test('idle and attachment-disabled states cannot send steering', async () => {
    await tick();
    control.update({ ...options, disabled: true });
    document.getElementById('steerButton').click();
    expect(request.mock.calls.some(([, opts]) => opts.method === 'POST')).toBe(false);
    control.update({ ...options, target: null });
    expect(document.getElementById('steerButton').hidden).toBe(true);
});

test('pending receipt survives turn completion and reports not applied', async () => {
    await tick();
    request.mockResolvedValue({ items: [{ id: 'id-1', text: 'Late steer', status: 'pending' }] });
    document.getElementById('steerButton').click();
    await tick();
    request.mockResolvedValue({ items: [{ id: 'id-1', text: 'Late steer', status: 'not_applied' }] });
    control.update({ ...options, target: null });
    await tick();
    expect(document.body.textContent).toContain('Not applied');
    expect(document.body.textContent).toContain('Late steer');
});

test('submit defaults to queue; Shift selects steering once without changing preference', async () => {
    await tick();
    control.update({ ...options, busy: true, actorKey: 'alice' });
    expect(control.activate()).toBe(false);
    expect(control.activate({ shiftKey: true })).toBe(true);
    await tick();
    expect(request.mock.calls.filter(([, opts]) => opts.method === 'POST')).toHaveLength(1);
    expect(localStorage.getItem('von:chat-submit-mode:alice')).toBeNull();
    expect(document.getElementById('chatSubmitMode').value).toBe('queue');
});

test('saved steering default is actor-scoped and never silently queues an unavailable draft', async () => {
    await tick();
    control.update({ ...options, busy: true, actorKey: 'alice' });
    const select = document.getElementById('chatSubmitMode');
    select.value = 'steer'; select.dispatchEvent(new Event('change'));
    expect(localStorage.getItem('von:chat-submit-mode:alice')).toBe('steer');
    expect(control.activate({ shiftKey: true })).toBe(false);
    control.update({ ...options, busy: true, actorKey: 'alice', disabled: true });
    expect(control.activate()).toBe(true);
    expect(draft).toBe('Correct the scope');
    expect(document.body.textContent).toContain('Steering is unavailable');
    expect(request.mock.calls.some(([, opts]) => opts.method === 'POST')).toBe(false);
    control.update({ ...options, busy: true, actorKey: 'bob', scopeKey: 'bob:session' });
    expect(select.value).toBe('queue');
    control.update({ ...options, busy: false, actorKey: 'alice' });
    expect(select.value).toBe('steer');
    expect(control.activate()).toBe(false); // Idle remains Send.
});

test('receipt notices expire without hiding recovery or repeating each poll', async () => {
    await tick();
    request.mockResolvedValue({ items: [{ id: 'late', text: 'Recover me', status: 'not_applied' }] });
    control.update({ ...options, target: { queueId: 'second', attemptId: 'attempt' } });
    await tick();
    const toast = document.querySelector('.chat-steering-toast');
    expect(toast.hidden).toBe(false);
    jest.advanceTimersByTime(5001);
    await tick();
    expect(toast.hidden).toBe(true);
    expect(document.querySelector('.chat-steering-feedback').textContent).toContain('Recover me');
    expect(document.querySelector('.chat-steering-activity').hidden).toBe(false);
});

test('storage failure retains explicit choice in memory across scope changes', async () => {
    await tick();
    const get = jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
    const set = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
    control.update({ ...options, busy: true, actorKey: 'alice' });
    const select = document.getElementById('chatSubmitMode');
    select.value = 'steer'; select.dispatchEvent(new Event('change'));
    control.update({ ...options, actorKey: 'bob' });
    control.update({ ...options, actorKey: 'alice' });
    expect(select.value).toBe('steer');
    get.mockRestore(); set.mockRestore();
});

test('unchanged polling preserves the focused cancellation control', async () => {
    await tick();
    request.mockResolvedValue({ items: [{ id: 'pending', text: 'Keep this', status: 'pending' }] });
    control.update({ ...options, target: { queueId: 'focus-queue', attemptId: 'attempt' } });
    await tick();
    const cancel = [...document.querySelectorAll('button')].find(node => node.textContent === 'Cancel steer');
    cancel.focus();
    jest.advanceTimersByTime(1500);
    await tick();
    expect(cancel.isConnected).toBe(true);
    expect(document.activeElement).toBe(cancel);
});

test('a new controller restores only the signed-in user’s stored default', () => {
    control.dispose();
    localStorage.setItem('von:chat-submit-mode:alice', 'steer');
    control = createChatSteeringControls({ sendButton: document.getElementById('send'), request,
        getDraft: () => draft, clearDraft: () => {}, createId: () => 'id' });
    control.update({ ...options, actorKey: 'alice', busy: true });
    expect(document.getElementById('chatSubmitMode').value).toBe('steer');
    expect(control.activate({ shiftKey: true })).toBe(false);
});
