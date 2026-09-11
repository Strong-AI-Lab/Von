/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/streamingSpeech.js', () => ({
    createStreamingInput: jest.fn(), createSpeechPlayback: jest.fn()
}));
const media = require('../../src/frontend/web/von_interface/static/js/streamingSpeech.js');
const { createVoiceConversation } = require('../../src/frontend/web/von_interface/static/js/voiceConversation.js');
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
let voice, input, callbacks, playback, context, submit;
beforeEach(() => {
    jest.clearAllMocks();
    document.body.innerHTML = '<button></button><p></p>';
    input = { start: jest.fn(async () => {}), cancel: jest.fn() };
    playback = { unlock: jest.fn(async () => {}), speak: jest.fn(async () => {}), stop: jest.fn(), dispose: jest.fn() };
    media.createStreamingInput.mockImplementation(options => { callbacks = options; return input; });
    media.createSpeechPlayback.mockReturnValue(playback);
    context = { key: 'actor:conversation', sessionId: 'conversation' };
    submit = jest.fn(async () => {});
    voice = createVoiceConversation({ button: document.querySelector('button'), status: document.querySelector('p'),
        getContext: () => context, onSubmit: submit, fetchImpl: jest.fn(async () => ({ ok: true })), root: window });
});
afterEach(() => voice.dispose());
test('duplicate final produces one submission; only latest utterance reply speaks', async () => {
    await voice.start(); callbacks.onFinal('First', 'one'); callbacks.onFinal('First', 'one'); await flush();
    expect(submit).toHaveBeenCalledTimes(1);
    const { attemptId } = submit.mock.calls[0][1];
    await voice.reply({ text: 'Answer', turnId: 'turn1', clientContext: { speech_attempt_ids: [attemptId], speech_item_id: 'one' } });
    expect(playback.speak).toHaveBeenCalledTimes(1);
    callbacks.onSpeechStart();
    expect(playback.stop).toHaveBeenCalledWith('barge_in');
    await voice.reply({ text: 'Old answer', turnId: 'late', clientContext: { speech_attempt_ids: [attemptId], speech_item_id: 'one' } });
    expect(playback.speak).toHaveBeenCalledTimes(1);
});
test('End cancels capture and playback; late final cannot submit', async () => {
    await voice.start(); voice.end(); callbacks.onFinal('late', 'one');
    expect(input.cancel).toHaveBeenCalled(); expect(playback.stop).toHaveBeenCalledWith('ended');
    expect(submit).not.toHaveBeenCalled();
});
test('next user turn clears the ended notice without stopping active voice', async () => {
    await voice.start();
    const status = document.querySelector('p');
    const activeStatus = status.textContent;
    voice.clearStatus();
    expect(status.textContent).toBe(activeStatus);
    expect(input.cancel).not.toHaveBeenCalled();
    callbacks.onFinal('Voice turn', 'one');
    voice.end();
    expect(status.textContent).toBe('Voice ended. Microphone released.');
    voice.clearStatus();
    expect(status.textContent).toBe('');
    callbacks.onPartial('late caption');
    expect(status.textContent).toBe('');
});
test('conversation switch ends capture before next final can write elsewhere', async () => {
    await voice.start(); context = { key: 'other', sessionId: 'other' }; callbacks.onFinal('wrong scope', 'one');
    expect(input.cancel).toHaveBeenCalled(); expect(submit).not.toHaveBeenCalled();
});
test('End while audio unlock waits prevents late microphone opening', async () => {
    let unlock; playback.unlock.mockReturnValue(new Promise(resolve => { unlock = resolve; }));
    const start = voice.start(); voice.end(); unlock(); await start;
    expect(media.createStreamingInput).not.toHaveBeenCalled();
});
