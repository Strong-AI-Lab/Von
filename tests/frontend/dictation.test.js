/** @jest-environment jsdom */
const { createDictationController, insertDictation, recordingMimeType } = require('../../src/frontend/web/von_interface/static/js/dictation.js');
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { resolve, promise }; };

describe('recorded dictation lifecycle', () => {
    let controller, recorder, stopTrack, getUserMedia, fetchImpl, input, context, root, pending, buttons;
    beforeEach(async () => {
        recorder = undefined;
        document.body.innerHTML = '<textarea></textarea><button id="dictate"></button><p></p><button id="cancel"></button><button id="retry"></button><select><option value="recorded">Recorded</option><option value="browser">Browser</option></select>';
        input = document.querySelector('textarea');
        input.value = 'Existing draft'; input.selectionStart = input.selectionEnd = input.value.length;
        context = { key: 'actor:conversation', text: 'Vontology and Wikidata', vocabulary: ['Vontology', 'Wikidata'], language: 'en-NZ' };
        stopTrack = jest.fn();
        getUserMedia = jest.fn(async () => ({ getTracks: () => [{ stop: stopTrack, addEventListener: jest.fn() }] }));
        class Recorder {
            static isTypeSupported(type) { return type === 'audio/mp4'; }
            constructor(stream, options) { recorder = this; this.mimeType = options.mimeType; this.state = 'inactive'; }
            start() { this.state = 'recording'; }
            stop() {
                this.state = 'inactive';
                this.ondataavailable({ data: new Blob(['recorded-audio'], { type: this.mimeType }) });
                this.onstop();
            }
        }
        root = { navigator: { mediaDevices: { getUserMedia } }, MediaRecorder: Recorder, isSecureContext: true };
        pending = deferred();
        fetchImpl = jest.fn(async url => url.endsWith('capabilities')
            ? { ok: true, json: async () => ({ transcription: { available: true, max_audio_bytes: 24 * 1024 * 1024 } }) }
            : pending.promise);
        buttons = [...document.querySelectorAll('button')];
        controller = createDictationController({ input, button: buttons[0], status: document.querySelector('p'), cancelButton: buttons[1], retryButton: buttons[2], engineSelect: document.querySelector('select'), getContext: () => context, setValue: value => { input.value = value; }, onStart: jest.fn(), fetchImpl, root });
        await flush();
    });
    afterEach(() => controller.dispose());
    test('negotiates Safari MP4 and preserves edits made during transcription', async () => {
        await controller.start();
        const finish = controller.finish();
        input.value = 'Edited while waiting';
        pending.resolve({ ok: true, json: async () => ({ text: 'Vontology works.' }) });
        expect(await finish).toBe(true);
        expect(input.value).toBe('Edited while waiting Vontology works.');
        expect(stopTrack).toHaveBeenCalled();
        const form = fetchImpl.mock.calls[1][1].body;
        expect(form.get('audio').type).toBe('audio/mp4');
        expect(form.get('vocabulary')).toContain('Wikidata');
        expect(form.get('language')).toBe('en-NZ');
    });
    test('cancel before permission returns releases late stream without recording', async () => {
        const permission = deferred(); getUserMedia.mockReturnValue(permission.promise);
        const start = controller.start();
        controller.cancel();
        permission.resolve({ getTracks: () => [{ stop: stopTrack }] });
        await start;
        expect(stopTrack).toHaveBeenCalled(); expect(recorder).toBeUndefined();
        expect(controller.getState()).toBe('idle');
    });
    test('late result after cancellation cannot modify draft', async () => {
        await controller.start(); const finish = controller.finish(); controller.cancel();
        pending.resolve({ ok: true, json: async () => ({ text: 'late transcript' }) });
        await flush(); expect(await finish).toBe(false); expect(input.value).toBe('Existing draft');
    });
    test('conversation change fences transcription result', async () => {
        await controller.start(); void controller.finish();
        context = { ...context, key: 'another conversation' };
        pending.resolve({ ok: true, json: async () => ({ text: 'wrong conversation' }) });
        await flush(); expect(input.value).toBe('Existing draft');
    });
    test('failed upload retains audio and retry does not reopen microphone', async () => {
        await controller.start(); const finish = controller.finish();
        pending.resolve({ ok: false, json: async () => ({ message: 'Network error. Retry.' }) });
        expect(await finish).toBe(false); expect(controller.getState()).toBe('error');
        expect(buttons[2].hidden).toBe(false); expect(input.value).toBe('Existing draft');
        pending = deferred(); buttons[2].click();
        pending.resolve({ ok: true, json: async () => ({ text: 'Recovered' }) });
        await flush(); expect(input.value).toBe('Existing draft Recovered');
        expect(getUserMedia).toHaveBeenCalledTimes(1);
    });
    test('permission denial has an actionable visible error', async () => {
        getUserMedia.mockRejectedValue({ name: 'NotAllowedError' });
        await controller.start();
        expect(document.querySelector('p').textContent).toContain('Microphone access was denied');
        expect(await controller.finish()).toBe(false);
    });
    test('inserts at selected range only when original draft is unchanged', () => {
        expect(insertDictation('Hello old world', 'new', { start: 6, end: 9 })).toBe('Hello new world');
        expect(recordingMimeType(root)).toBe('audio/mp4');
    });
});

describe('browser recognition snapshots', () => {
    let recognition;
    afterEach(() => { delete global.SpeechRecognition; });
    test('replayed final results are a complete snapshot, not duplicate deltas', () => {
        global.SpeechRecognition = class { constructor() { recognition = this; } start() {} };
        const onResult = jest.fn();
        require('../../src/frontend/web/von_interface/static/js/speech.js').startSpeechRecognition({ onResult });
        const final = text => Object.assign([{ transcript: text }], { isFinal: true });
        recognition.onresult({ resultIndex: 0, results: [final('Von')] });
        recognition.onresult({ resultIndex: 0, results: [final('Von'), final('Vontology')] });
        recognition.onresult({ resultIndex: 1, results: [final('Von'), final('Vontology')] });
        expect(onResult.mock.calls.map(call => call[0].finalText)).toEqual(['Von', 'Von Vontology', 'Von Vontology']);
        // DGX report: progressively longer prefixes were appended repeatedly.
        for (const text of ['it', 'it seems', 'it seems that', 'it seems that the speech']) {
            recognition.onresult({ resultIndex: 0, results: [final(text)] });
        }
        expect(onResult.mock.calls.at(-1)[0].finalText).toBe('it seems that the speech');
    });
});
