/** @jest-environment jsdom */

const speechModulePath = '../../src/frontend/web/von_interface/static/js/speech.js';

const { getSpeechSynthesisVoices, speakText, stopSpeaking } = require(speechModulePath);

describe('speech TTS utilities', () => {
    afterEach(() => {
        stopSpeaking();
        delete global.speechSynthesis;
        delete global.SpeechSynthesisUtterance;
        jest.restoreAllMocks();
        jest.useRealTimers();
    });

    test.each([
        ['Android Chrome/130 Mobile', 1],
        ['iPhone Mobile Safari', 1],
        ['Macintosh Safari', 5]
    ])('mobile %s speaks in the gesture and never runs Chromium pause/resume', (userAgent, maxTouchPoints) => {
        jest.useFakeTimers();
        jest.spyOn(navigator, 'userAgent', 'get').mockReturnValue(userAgent);
        Object.defineProperty(navigator, 'maxTouchPoints', { configurable: true, get: () => maxTouchPoints });
        global.speechSynthesis = { cancel: jest.fn(), speak: jest.fn(), getVoices: () => [], speaking: true, paused: false, pause: jest.fn(), resume: jest.fn() };
        global.SpeechSynthesisUtterance = function (text) { this.text = text; };
        speakText('A longer conversational reply.');
        expect(global.speechSynthesis.speak).toHaveBeenCalledTimes(1);
        jest.advanceTimersByTime(30000);
        expect(global.speechSynthesis.pause).not.toHaveBeenCalled();
        expect(global.speechSynthesis.resume).not.toHaveBeenCalled();
    });

    test('getSpeechSynthesisVoices returns empty array when unsupported', () => {
        expect(getSpeechSynthesisVoices()).toEqual([]);
    });

    test('speakText applies voiceUri when available', async () => {
        const voice = { voiceURI: 'voice-1', name: 'Test voice', lang: 'en-NZ' };

        global.speechSynthesis = {
            cancel: jest.fn(),
            speak: jest.fn(),
            getVoices: jest.fn(() => [voice])
        };

        global.SpeechSynthesisUtterance = function SpeechSynthesisUtterance(text) {
            this.text = text;
            this.lang = '';
            this.rate = 1;
            this.pitch = 1;
            this.volume = 1;
            this.voice = null;
        };

        const utterance = speakText('hello', {
            voiceUri: 'voice-1',
            language: 'en-NZ',
            rate: 1.2,
            pitch: 0.9,
            volume: 0.8
        });

        expect(global.speechSynthesis.cancel).toHaveBeenCalled();

        // speech.js intentionally delays speak() briefly after cancel() to avoid
        // first-words clipping in some browsers.
        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(global.speechSynthesis.speak).toHaveBeenCalledWith(utterance);
        expect(utterance.lang).toBe('en-NZ');
        expect(utterance.rate).toBe(1.2);
        expect(utterance.pitch).toBe(0.9);
        expect(utterance.volume).toBe(0.8);
        expect(utterance.voice).toBe(voice);
    });
});
