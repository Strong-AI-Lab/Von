import { isSpeechRecognitionSupported, startSpeechRecognition, stopSpeaking } from './speech.js';

const RECORDING_TYPES = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'];

export function recordingMimeType(root = globalThis) {
    return RECORDING_TYPES.find(type => root.MediaRecorder?.isTypeSupported?.(type)) || '';
}

export function supportsAudioRecording(root = globalThis) {
    return root.isSecureContext !== false && typeof root.MediaRecorder === 'function'
        && typeof root.navigator?.mediaDevices?.getUserMedia === 'function';
}

export function insertDictation(value, text, selection = null) {
    if (!text) return value;
    const start = selection?.start ?? value.length;
    const end = selection?.end ?? start;
    const before = value.slice(0, start);
    const after = value.slice(end);
    return `${before}${before && !/\s$/.test(before) ? ' ' : ''}${text}${after && !/^\s|^[,.;:!?]/.test(after) ? ' ' : ''}${after}`;
}

function microphoneError(error) {
    if (['NotAllowedError', 'not-allowed', 'service-not-allowed'].includes(error?.name || error?.error)) {
        return 'Microphone access was denied. Allow microphone access in your browser and try again.';
    }
    if (error?.name === 'NotFoundError') return 'No microphone is available.';
    if (error?.error === 'no-speech') return 'No speech was detected. Try again.';
    return error?.message || 'Speech recognition stopped. Try recorded audio or type your message.';
}

// One controller per composer. A capture belongs to the conversation and actor
// that started it. Cancelling fences permission, recognition and fetch callbacks.
export function createDictationController({ input, button, status, cancelButton, retryButton,
    engineSelect, getContext, setValue, onStart = stopSpeaking, fetchImpl = (...args) => fetch(...args),
    root = globalThis }) {
    let current = null;
    let capability = null;
    let state = 'idle';
    let disposed = false;

    function render(next, message = '') {
        state = next;
        const busy = ['requesting', 'recording', 'transcribing'].includes(next);
        button.textContent = next === 'recording' ? 'Finish dictation' : next === 'transcribing' ? 'Transcribing…' : next === 'requesting' ? 'Opening microphone…' : 'Dictate';
        button.disabled = next === 'transcribing' || next === 'requesting';
        button.setAttribute('aria-pressed', String(next === 'recording'));
        button.classList.toggle('active-dictation', busy);
        status.textContent = message;
        cancelButton.hidden = !current;
        retryButton.hidden = !(next === 'error' && current?.blob);
        engineSelect.disabled = !!current;
    }

    function valid(capture) {
        if (!disposed && capture === current && capture.key !== getContext().key) cancel();
        return !disposed && capture === current;
    }

    function release(capture) {
        capture.stream?.getTracks().forEach(track => track.stop());
    }

    function cancel() {
        const capture = current;
        current = null;
        if (capture) {
            capture.abort?.abort();
            capture.recognition?.abort();
            if (capture.recorder?.state === 'recording') capture.recorder.stop();
            release(capture);
            capture.resolve?.(false);
        }
        render('idle');
    }

    function fail(capture, message) {
        if (!valid(capture)) return;
        release(capture);
        render('error', message);
        capture.resolve?.(false);
    }

    function commit(capture, text) {
        if (!valid(capture)) return;
        const value = String(input.value || '');
        const selection = value === capture.base ? capture.selection : null;
        setValue(insertDictation(value, text.trim(), selection));
        current = null;
        render('idle', text.trim() ? 'Dictation added. Edit it or send when ready.' : 'No speech was detected. Try again.');
        capture.resolve?.(!!text.trim());
    }

    async function transcribe(capture) {
        if (!valid(capture)) return;
        render('transcribing', 'Transcribing with conversation vocabulary…');
        capture.abort = new AbortController();
        const form = new FormData();
        const mime = capture.blob.type.split(';')[0];
        form.append('audio', capture.blob, mime === 'audio/mp4' ? 'dictation.mp4' : 'dictation.webm');
        form.append('language', capture.context.language || '');
        form.append('context', capture.context.text || '');
        form.append('vocabulary', JSON.stringify(capture.context.vocabulary || []));
        try {
            const response = await fetchImpl('/api/speech/transcribe', {
                method: 'POST', body: form, credentials: 'same-origin', signal: capture.abort.signal
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.message || 'Transcription failed. Retry your recording.');
            if (typeof data.text !== 'string') throw new Error('No transcript was returned. Retry your recording.');
            commit(capture, data.text);
        } catch (error) {
            if (valid(capture)) fail(capture, error.message || 'Connection lost. Retry your recording.');
        }
    }

    async function start() {
        if (current) cancel();
        const context = getContext();
        const capture = { key: context.key, context, base: input.value,
            selection: { start: input.selectionStart, end: input.selectionEnd }, chunks: [], bytes: 0 };
        capture.completion = new Promise(resolve => { capture.resolve = resolve; });
        current = capture;
        onStart();
        render('requesting', 'Allow microphone access to dictate.');
        try {
            if (engineSelect.value === 'browser') {
                if (!isSpeechRecognitionSupported()) throw new Error('Browser recognition is unavailable. Choose recorded audio.');
                capture.recognition = startSpeechRecognition({
                    language: context.language, continuous: context.continuous ?? false, interimResults: context.interimResults ?? true,
                    vocabulary: context.vocabulary,
                    onResult: ({ finalText, interimText }) => {
                        if (!valid(capture)) return;
                        capture.finalText = finalText;
                        status.textContent = [finalText, interimText].filter(Boolean).join(' ') || 'Listening…';
                    },
                    onError: error => { capture.failed = true; fail(capture, microphoneError(error)); },
                    onEnd: () => { if (!capture.failed) commit(capture, capture.finalText || ''); }
                });
            } else {
                if (!supportsAudioRecording(root)) throw new Error('Recording requires HTTPS and a browser with microphone recording support.');
                if (!capability?.available) throw new Error(capability?.reason || 'Recorded transcription is unavailable. Try browser recognition or check Speech settings.');
                capture.stream = await root.navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
                if (!valid(capture)) { release(capture); return; }
                const mimeType = recordingMimeType(root);
                capture.recorder = new root.MediaRecorder(capture.stream, mimeType ? { mimeType } : undefined);
                capture.recorder.ondataavailable = event => {
                    if (!valid(capture) || !event.data?.size) return;
                    capture.chunks.push(event.data);
                    capture.bytes += event.data.size;
                    if (capture.bytes >= capability.max_audio_bytes * 0.9 && capture.recorder.state === 'recording') {
                        status.textContent = 'Finishing this recording before the upload size limit.';
                        capture.recorder.stop();
                    }
                };
                capture.recorder.onerror = error => {
                    capture.failed = true;
                    fail(capture, microphoneError(error));
                };
                capture.recorder.onstop = () => {
                    release(capture);
                    if (!valid(capture) || capture.failed) return;
                    capture.blob = new Blob(capture.chunks, { type: capture.recorder.mimeType || mimeType });
                    capture.chunks = [];
                    if (!capture.blob.size) { fail(capture, 'No audio was recorded. Try again.'); return; }
                    void transcribe(capture);
                };
                capture.stream.getTracks().forEach(track => track.addEventListener?.('ended', () => {
                    if (valid(capture) && capture.recorder.state === 'recording') capture.recorder.stop();
                }));
                capture.recorder.start(1000);
            }
            if (valid(capture) && state !== 'error') render('recording', 'Listening. Select Finish dictation when you are ready.');
        } catch (error) {
            fail(capture, microphoneError(error));
        }
    }

    async function finish() {
        if (!current) return true;
        if (state === 'error') return false;
        const capture = current;
        if (capture.recorder?.state === 'recording') capture.recorder.stop();
        capture.recognition?.stop();
        if (state === 'requesting') { cancel(); return false; }
        return capture.completion;
    }

    async function refreshCapabilities() {
        try {
            const response = await fetchImpl('/api/speech/capabilities', { credentials: 'same-origin', cache: 'no-store' });
            const data = await response.json();
            capability = response.ok ? data.transcription : { available: false, reason: 'Sign in to use recorded transcription.' };
        } catch (_) {
            capability = { available: false, reason: 'Unable to check recorded transcription. Try again when connected.' };
        }
        if (!disposed && !current) {
            render('idle', capability?.available
                ? 'Recorded audio and visible conversation context are sent to OpenAI for transcription. Audio is not saved by Von.'
                : capability?.reason || 'Recorded transcription is unavailable.');
        }
    }

    const click = () => { if (state === 'recording') void finish(); else void start(); };
    const retry = () => { if (current?.blob) {
        current.completion = new Promise(resolve => { current.resolve = resolve; });
        void transcribe(current);
    } };
    const visibility = () => { if (root.document?.hidden && current) void finish(); };
    button.addEventListener('click', click);
    cancelButton.addEventListener('click', cancel);
    retryButton.addEventListener('click', retry);
    root.addEventListener?.('pagehide', cancel);
    root.addEventListener?.('focus', refreshCapabilities);
    root.addEventListener?.('von-preferences-changed', refreshCapabilities);
    root.document?.addEventListener('visibilitychange', visibility);
    render('idle');
    void refreshCapabilities();
    return { cancel, finish, start, refreshCapabilities, getState: () => state,
        dispose: () => { cancel(); disposed = true;
            button.removeEventListener('click', click); cancelButton.removeEventListener('click', cancel);
            retryButton.removeEventListener('click', retry); root.removeEventListener?.('pagehide', cancel);
            root.removeEventListener?.('focus', refreshCapabilities);
            root.removeEventListener?.('von-preferences-changed', refreshCapabilities);
            root.document?.removeEventListener('visibilitychange', visibility);
        } };
}
