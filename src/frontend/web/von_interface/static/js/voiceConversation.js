import { createStreamingInput, createSpeechPlayback } from './streamingSpeech.js';
import { createSpeechReporter } from './clientContext.js';

export function createVoiceConversation({ button, status, getContext, onSubmit, onStart = () => {}, onActiveChange = () => {},
    fetchImpl = (...args) => fetch(...args), root = globalThis }) {
    let session = null, disposed = false, starting = false, generation = 0;
    const playback = createSpeechPlayback({ fetchImpl, root,
        onEvent: (name, values) => session?.reporter.event(name, { ...values, item_id: session.playbackItem }) });
    function valid(value) {
        if (value === session && getContext().key !== value.context.key) end('Conversation changed. Voice ended.');
        return !disposed && session === value;
    }
    function end(message = 'Voice ended. Microphone released.') {
        generation++; starting = false;
        const old = session;
        old?.reporter.event('ended', { reason: 'ended' });
        old?.input.cancel(); playback.stop('ended');
        session = null;
        onActiveChange(false);
        button.textContent = 'Start voice'; button.setAttribute('aria-pressed', 'false');
        status.textContent = message;
    }
    async function start() {
        if (session || starting) return end();
        starting = true; const token = ++generation;
        onActiveChange(true);
        button.textContent = 'End voice'; button.setAttribute('aria-pressed', 'true');
        // Resume audio in the initiating gesture, before permission/network awaits.
        try { await playback.unlock(); } catch (_) { if (generation === token) end('This browser could not open audio playback.'); return; }
        if (generation !== token || disposed) return;
        try { await onStart(); }
        catch (error) { if (generation === token) end(error.message); return; }
        if (disposed || generation !== token) return;
        starting = false;
        const context = getContext();
        const value = { context, submitted: new Set(), latestItem: null, heard: new Set() };
        value.reporter = createSpeechReporter({ fetchImpl, getContext: () => value.context, root });
        value.input = createStreamingInput({ context, fetchImpl, root, audioContext: playback.getAudioContext?.(),
            onState: (state, message) => {
                if (!valid(value)) return;
                status.textContent = message;
                value.reporter.event('state', { state });
                if (state === 'error') end(message + ' Select Start voice to reconnect, or use Dictate.');
            },
            onPartial: text => { if (valid(value)) status.textContent = text; },
            onSpeechStart: () => {
                if (!valid(value)) return;
                // Invalidate late spoken replies; already-running tools retain
                // their canonical results and are never blindly repeated.
                value.latestItem = null;
                playback.stop('barge_in');
                value.reporter.event('interrupted', { reason: 'user_speech' });
            },
            onEvent: (name, values) => value.reporter.event(name, values),
            onFinal: (text, itemId) => {
                if (!valid(value) || !text.trim() || value.submitted.has(itemId)) return;
                value.submitted.add(itemId); value.latestItem = itemId;
                status.textContent = 'Von is responding. You can keep speaking.';
                void onSubmit(text, {
                    attemptId: value.reporter.attemptId, itemId,
                    submissionId: value.reporter.attemptId + '-' + itemId
                }).then(() => {
                    value.reporter.event('submitted', { item_id: itemId, submission_id: value.reporter.attemptId + '-' + itemId });
                }).catch(() => {
                    if (valid(value)) end('Your message remains in the conversation queue for recovery. Voice paused.');
                });
            }
        });
        session = value;
        button.textContent = 'End voice'; button.setAttribute('aria-pressed', 'true');
        status.textContent = 'Opening voice. Audio is sent to OpenAI; Von’s voice is AI-generated.';
        try { await value.input.start(); }
        catch (_) { /* Input exposes a recoverable error through onState. */ }
    }
    async function reply({ text, turnId, clientContext }) {
        const value = session;
        if (!value || !valid(value) || !text?.trim() || value.heard.has(turnId)) return false;
        // Only the reply to this session's latest submitted utterance may speak.
        if (!clientContext?.speech_attempt_ids?.includes(value.reporter.attemptId)
            || clientContext.speech_item_id !== value.latestItem) return false;
        value.heard.add(turnId);
        value.playbackItem = value.latestItem;
        value.input.updateContext?.(getContext());
        status.textContent = 'Von is speaking. Speak to interrupt.';
        try { await playback.speak(text); if (valid(value)) status.textContent = 'Listening…'; }
        catch (error) { if (valid(value)) status.textContent = error.message; }
        return true;
    }
    const click = () => { if (session || starting) end(); else void start(); };
    const leave = () => end();
    const hide = () => { if (root.document?.hidden && (session || starting)) end('Voice paused while the page is hidden. Start voice to resume.'); };
    const shortcut = event => {
        if (event.altKey && event.shiftKey && event.code === 'KeyV') { event.preventDefault(); click(); }
    };
    button.addEventListener('click', click);
    root.document?.addEventListener('visibilitychange', hide);
    root.addEventListener?.('pagehide', leave);
    root.document?.addEventListener('keydown', shortcut);
    return { start, end, reply, isActive: () => !!session,
        dispose() { end(); disposed = true; playback.dispose(); button.removeEventListener('click', click);
            root.document?.removeEventListener('visibilitychange', hide); root.removeEventListener?.('pagehide', leave);
            root.document?.removeEventListener('keydown', shortcut); }
    };
}
