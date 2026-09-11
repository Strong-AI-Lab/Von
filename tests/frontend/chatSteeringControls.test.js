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
