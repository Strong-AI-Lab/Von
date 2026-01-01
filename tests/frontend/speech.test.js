/** @jest-environment jsdom */

const speechModulePath = '../../src/frontend/web/von_interface/static/js/speech.js';

const { getSpeechSynthesisVoices, speakText } = require(speechModulePath);

describe('speech TTS utilities', () => {
    afterEach(() => {
        delete global.speechSynthesis;
        delete global.SpeechSynthesisUtterance;
        jest.restoreAllMocks();
    });

    test('getSpeechSynthesisVoices returns empty array when unsupported', () => {
        expect(getSpeechSynthesisVoices()).toEqual([]);
    });

    test('speakText applies voiceUri when available', () => {
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
        expect(global.speechSynthesis.speak).toHaveBeenCalledWith(utterance);
        expect(utterance.lang).toBe('en-NZ');
        expect(utterance.rate).toBe(1.2);
        expect(utterance.pitch).toBe(0.9);
        expect(utterance.volume).toBe(0.8);
        expect(utterance.voice).toBe(voice);
    });
});
