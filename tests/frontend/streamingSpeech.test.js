/** @jest-environment jsdom */
const { createStreamingInput, createSpeechPlayback } = require('../../src/frontend/web/von_interface/static/js/streamingSpeech.js');
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
function setup(overrides = {}) {
    let peer;
    const track = { enabled: true, stop: jest.fn(), addEventListener: jest.fn() };
    const channel = { readyState: 'open', send: jest.fn(), close: jest.fn() };
    class Peer {
        constructor() { peer = this; }
        addTrack() {}
        createDataChannel() { return channel; }
        async createOffer() { return { sdp: 'v=0\r\nfixture' }; }
        async setLocalDescription() {}
        async setRemoteDescription() { channel.onopen(); }
        close() {}
    }
    class AudioContext {
        state = 'running';
        async resume() {}
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
        createAnalyser() { return { fftSize: 1024, getFloatTimeDomainData() {}, disconnect() {} }; }
        close() {}
    }
    const root = { navigator: { mediaDevices: { getUserMedia: jest.fn(async () => ({ getTracks: () => [track] })) } },
        RTCPeerConnection: Peer, AudioContext, setTimeout, clearTimeout, setInterval, clearInterval, performance, isSecureContext: true, ...overrides };
    const onFinal = jest.fn(), onPartial = jest.fn(), onState = jest.fn();
    const fetchImpl = jest.fn(async () => ({ ok: true, json: async () => ({ sdp: 'v=0\r\nanswer', model: 'gpt-live-transcribe' }) }));
    const input = createStreamingInput({ context: { text: 'Vontology', vocabulary: ['Von'], language: 'en-NZ' }, root, fetchImpl, onFinal, onPartial, onState });
    return { input, root, track, channel, onFinal, onPartial, onState, fetchImpl, peer: () => peer };
}
const event = (input, type, item_id, fields = {}) => input.receive({ type, item_id, ...fields });
test('reordered completions and duplicate finals emit exactly once in committed order', async () => {
    const { input, onFinal } = setup(); await input.start();
    event(input, 'input_audio_buffer.committed', 'one'); event(input, 'input_audio_buffer.committed', 'two');
    event(input, 'conversation.item.input_audio_transcription.completed', 'two', { transcript: 'second' });
    expect(onFinal).not.toHaveBeenCalled();
    event(input, 'conversation.item.input_audio_transcription.completed', 'one', { transcript: 'first' });
    event(input, 'conversation.item.input_audio_transcription.completed', 'one', { transcript: 'first' });
    expect(onFinal.mock.calls).toEqual([['first', 'one'], ['second', 'two']]); input.cancel();
});
test('finish waits for commit acknowledgement and final transcript, then stops tracks', async () => {
    const { input, channel, track } = setup(); await input.start();
    const finish = input.finish(); let finished = false; finish.then(() => { finished = true; });
    await flush(); expect(finished).toBe(false);
    expect(JSON.parse(channel.send.mock.calls[0][0]).type).toBe('input_audio_buffer.commit');
    event(input, 'input_audio_buffer.committed', 'one');
    event(input, 'conversation.item.input_audio_transcription.completed', 'one', { transcript: 'Von' });
    expect(await finish).toBe('Von'); expect(track.stop).toHaveBeenCalled();
});
test('cancel while permission is outstanding releases a late microphone and makes no provider call', async () => {
    let grant; const permission = new Promise(resolve => { grant = resolve; });
    const { input, root, track, fetchImpl } = setup(); root.navigator.mediaDevices.getUserMedia.mockReturnValue(permission);
    const start = input.start(); await flush(); input.cancel(); grant({ getTracks: () => [track] }); await start;
    expect(track.stop).toHaveBeenCalled(); expect(fetchImpl).not.toHaveBeenCalled();
});
test('transport failure closes microphone and prevents late transcript callbacks', async () => {
    const { input, track, onFinal, peer } = setup(); await input.start();
    peer().connectionState = 'failed'; peer().onconnectionstatechange();
    event(input, 'input_audio_buffer.committed', 'late');
    event(input, 'conversation.item.input_audio_transcription.completed', 'late', { transcript: 'late' });
    expect(track.stop).toHaveBeenCalled(); expect(onFinal).not.toHaveBeenCalled();
});
test('cancel during output unlock cannot issue a late synthesis request', async () => {
    let unlock;
    const resume = new Promise(resolve => { unlock = resolve; });
    class AudioContext { constructor() { this.state = 'running'; } resume() { return resume; } close() {} }
    const fetchImpl = jest.fn();
    const player = createSpeechPlayback({ fetchImpl, root: { AudioContext } });
    const speaking = player.speak('A response'); player.stop(); unlock(); await speaking;
    expect(fetchImpl).not.toHaveBeenCalled(); player.dispose();
});

test('short gaps and isolated clicks do not commit; a sustained utterance followed by a pause commits once', async () => {
    jest.useFakeTimers();
    let level = 0;
    const analyser = { fftSize: 1024, getFloatTimeDomainData: samples => samples.fill(level), disconnect() {} };
    class AudioContext {
        state = 'running';
        async resume() {}
        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
        createAnalyser() { return analyser; }
        close() {}
    }
    const { input, channel } = setup({ AudioContext, performance: { now: () => Date.now() } });
    try {
        await input.start();
        level = 0.1; jest.advanceTimersByTime(60); level = 0; jest.advanceTimersByTime(1200);
        expect(channel.send).not.toHaveBeenCalled();
        level = 0.1; jest.advanceTimersByTime(600); level = 0; jest.advanceTimersByTime(300);
        level = 0.1; jest.advanceTimersByTime(500);
        expect(channel.send).not.toHaveBeenCalled();
        level = 0; jest.advanceTimersByTime(1100); jest.advanceTimersByTime(2000);
        expect(channel.send).toHaveBeenCalledTimes(1);
    } finally { input.cancel(); jest.useRealTimers(); }
});
