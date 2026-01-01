// Speech utilities (browser-only)
// - STT: Web Speech API (SpeechRecognition)
// - TTS: SpeechSynthesis
//
// Keep this module safe to import in test environments (no top-level browser-only refs).

function getSpeechRecognitionCtor() {
    const root = typeof globalThis !== 'undefined' ? globalThis : null;
    if (!root) {
        return null;
    }

    return root.SpeechRecognition || root.webkitSpeechRecognition || null;
}

export function isSpeechRecognitionSupported() {
    return typeof getSpeechRecognitionCtor() === 'function';
}

export function startSpeechRecognition(options = {}) {
    const {
        language,
        continuous = true,
        interimResults = true,
        onResult,
        onError,
        onEnd
    } = options;

    const SpeechRecognitionCtor = getSpeechRecognitionCtor();
    if (!SpeechRecognitionCtor) {
        throw new Error('SpeechRecognition is not supported in this browser.');
    }

    const recognition = new SpeechRecognitionCtor();
    recognition.continuous = !!continuous;
    recognition.interimResults = !!interimResults;

    if (language) {
        recognition.lang = String(language);
    }

    recognition.onresult = (event) => {
        if (typeof onResult !== 'function') {
            return;
        }

        try {
            let finalText = '';
            let interimText = '';

            for (let i = event.resultIndex; i < event.results.length; i += 1) {
                const result = event.results[i];
                if (!result || !result[0]) {
                    continue;
                }

                const transcript = String(result[0].transcript || '');
                if (result.isFinal) {
                    finalText += transcript;
                } else {
                    interimText += transcript;
                }
            }

            onResult({
                finalText: finalText.trim(),
                interimText: interimText.trim(),
                event
            });
        } catch (err) {
            // Best-effort: swallow and keep recognition alive.
        }
    };

    recognition.onerror = (event) => {
        if (typeof onError === 'function') {
            onError(event);
        }
    };

    recognition.onend = () => {
        if (typeof onEnd === 'function') {
            onEnd();
        }
    };

    recognition.start();

    return {
        stop: () => {
            try {
                recognition.stop();
            } catch (_) {
                // Ignore.
            }
        },
        abort: () => {
            try {
                recognition.abort();
            } catch (_) {
                // Ignore.
            }
        }
    };
}

export function isTextToSpeechSupported() {
    const root = typeof globalThis !== 'undefined' ? globalThis : null;
    return !!(root && root.speechSynthesis && typeof root.SpeechSynthesisUtterance === 'function');
}

export function getSpeechSynthesisVoices() {
    const root = typeof globalThis !== 'undefined' ? globalThis : null;
    if (!root || !root.speechSynthesis || typeof root.speechSynthesis.getVoices !== 'function') {
        return [];
    }

    try {
        const voices = root.speechSynthesis.getVoices();
        return Array.isArray(voices) ? voices : [];
    } catch (_) {
        return [];
    }
}

function findSpeechSynthesisVoiceByUri(voiceUri) {
    if (!voiceUri) {
        return null;
    }

    const uri = String(voiceUri);
    const voices = getSpeechSynthesisVoices();
    return voices.find((v) => v && String(v.voiceURI || '') === uri) || null;
}

export function stopSpeaking() {
    const root = typeof globalThis !== 'undefined' ? globalThis : null;
    if (!root || !root.speechSynthesis) {
        return;
    }

    try {
        root.speechSynthesis.cancel();
    } catch (_) {
        // Ignore.
    }
}

export function speakText(text, options = {}) {
    const root = typeof globalThis !== 'undefined' ? globalThis : null;
    if (!root || !root.speechSynthesis || typeof root.SpeechSynthesisUtterance !== 'function') {
        throw new Error('Text-to-speech is not supported in this browser.');
    }

    const utterance = new root.SpeechSynthesisUtterance(String(text ?? ''));

    if (options.voiceUri) {
        const voice = findSpeechSynthesisVoiceByUri(options.voiceUri);
        if (voice) {
            try {
                utterance.voice = voice;
            } catch (_) {
                // Ignore: some environments may not allow setting voice.
            }
        }
    }

    if (options.language) {
        utterance.lang = String(options.language);
    }
    if (typeof options.rate === 'number') {
        utterance.rate = options.rate;
    }
    if (typeof options.pitch === 'number') {
        utterance.pitch = options.pitch;
    }
    if (typeof options.volume === 'number') {
        utterance.volume = options.volume;
    }

    // Prefer immediate playback (and avoid queueing multiple long utterances).
    try {
        root.speechSynthesis.cancel();
    } catch (_) {
        // Ignore.
    }

    root.speechSynthesis.speak(utterance);
    return utterance;
}
