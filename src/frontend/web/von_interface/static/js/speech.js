// Speech utilities (browser-only)
// - STT: Web Speech API (SpeechRecognition)
// - TTS: SpeechSynthesis
//
// Keep this module safe to import in test environments (no top-level browser-only refs).

const DEFAULT_CANCEL_SETTLE_MS = 120;

// Chrome's Web Speech API has a bug where long utterances silently stop after ~15 seconds.
// Workaround: periodically call pause()/resume() to keep the speech alive.
const KEEPALIVE_INTERVAL_MS = 10000;

let pendingSpeakTimerId = null;
let keepaliveIntervalId = null;

function clearPendingSpeakTimer(root) {
    if (!pendingSpeakTimerId) {
        return;
    }

    try {
        root?.clearTimeout?.(pendingSpeakTimerId);
    } catch (_) {
        // Ignore.
    }

    pendingSpeakTimerId = null;
}

function clearKeepaliveInterval(root) {
    if (!keepaliveIntervalId) {
        return;
    }

    try {
        root?.clearInterval?.(keepaliveIntervalId);
    } catch (_) {
        // Ignore.
    }

    keepaliveIntervalId = null;
}

function startKeepaliveInterval(root) {
    clearKeepaliveInterval(root);
    // Mobile pause/resume can terminate or restart speech. Restrict the
    // keepalive workaround to the affected desktop Chromium implementation.
    const ua = root?.navigator?.userAgent || '';
    if (!/Chrome\//.test(ua) || /Android|Mobile|iPhone|iPad|iPod/.test(ua)
        || (root?.navigator?.maxTouchPoints > 1 && /Macintosh/.test(ua))) return;

    if (!root || typeof root.setInterval !== 'function') {
        return;
    }

    keepaliveIntervalId = root.setInterval(() => {
        try {
            // Chrome workaround: pause/resume to keep long utterances alive.
            if (root.speechSynthesis && root.speechSynthesis.speaking && !root.speechSynthesis.paused) {
                root.speechSynthesis.pause();
                root.speechSynthesis.resume();
            }
        } catch (_) {
            // Ignore errors during keepalive.
        }
    }, KEEPALIVE_INTERVAL_MS);
}

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
        vocabulary = [],
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
    const Phrase = globalThis.SpeechRecognitionPhrase;
    if ('phrases' in recognition && typeof Phrase === 'function') {
        try {
            recognition.phrases = vocabulary.slice(0, 80).map(term => new Phrase(String(term), 3));
        } catch (_) { /* Optional contextual biasing must not break capture. */ }
    }

    if (language) {
        recognition.lang = String(language);
    }

    recognition.onresult = (event) => {
        if (typeof onResult !== 'function') {
            return;
        }

        try {
            // Each event is a revisable snapshot, including replayed finals.
            let finalText = '';
            let interimText = '';

            for (let i = 0; i < event.results.length; i += 1) {
                const result = event.results[i];
                if (!result || !result[0]) {
                    continue;
                }

                const transcript = String(result[0].transcript || '');
                if (result.isFinal) {
                    finalText += ` ${transcript}`;
                } else {
                    interimText += ` ${transcript}`;
                }
            }

            onResult({
                finalText: finalText.trim(),
                interimText: interimText.trim(),
                event
            });
        } catch (_err) {
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

    clearPendingSpeakTimer(root);
    clearKeepaliveInterval(root);

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

    clearPendingSpeakTimer(root);
    clearKeepaliveInterval(root);

    // Prompt browsers (notably Chrome) to load voices early.
    try {
        root.speechSynthesis.getVoices?.();
    } catch (_) {
        // Ignore.
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

    // Attach event listeners to clear keepalive when speech ends or errors.
    const cleanupKeepalive = () => clearKeepaliveInterval(root);
    if (typeof utterance.addEventListener === 'function') {
        utterance.addEventListener('end', cleanupKeepalive);
        utterance.addEventListener('error', cleanupKeepalive);
    } else {
        utterance.onend = cleanupKeepalive;
        utterance.onerror = cleanupKeepalive;
    }

    // Prefer immediate playback (and avoid queueing multiple long utterances).
    try {
        root.speechSynthesis.cancel();
    } catch (_) {
        // Ignore.
    }

    // Workaround for Web Speech quirks where calling speak() immediately after cancel()
    // can clip the first words of an utterance.
    const settleMs =
        typeof options.cancelSettleMs === 'number'
            ? options.cancelSettleMs
            : (/Android|Mobile|iPhone|iPad|iPod/.test(root.navigator?.userAgent || '')
                || (root.navigator?.maxTouchPoints > 1 && /Macintosh/.test(root.navigator?.userAgent || '')))
                ? 0 : DEFAULT_CANCEL_SETTLE_MS;

    const doSpeak = () => {
        pendingSpeakTimerId = null;
        try {
            root.speechSynthesis.speak(utterance);
            // Start the Chrome keepalive workaround after speech begins.
            startKeepaliveInterval(root);
        } catch (error) {
            utterance.onerror?.({ error: 'synthesis-failed', message: error?.message });
        }
    };

    if (settleMs > 0 && typeof root.setTimeout === 'function') {
        pendingSpeakTimerId = root.setTimeout(doSpeak, settleMs);
    } else {
        doSpeak();
    }
    return utterance;
}
