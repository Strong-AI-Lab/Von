// WebRTC only transports input audio. Von's existing turn runtime owns replies/tools.
export function createStreamingInput({ context, fetchImpl, onPartial, onFinal, onSpeechStart,
    onState = () => {}, onEvent = () => {}, root = globalThis, audioContext: suppliedAudioContext }) {
    let peer, channel, stream, cancelled = false, finishing = false, speaking = false;
    let resolveFinish, rejectFinish, connectTimer;
    const abort = new AbortController();
    const items = new Map();
    const order = [];
    let emitted = 0, finishAcknowledged = false;
    let model = null;
    let audioContext = suppliedAudioContext, source, analyser, levelTimer, finishTimer;
    let voiceSince = null, lastVoiceAt = null, noise = 0.003;
    const completion = new Promise((resolve, reject) => { resolveFinish = resolve; rejectFinish = reject; });
    // Completion is also observed by callers that finish later.
    completion.catch(() => {});
    function clean() {
        root.clearTimeout?.(connectTimer);
        root.clearTimeout?.(finishTimer);
        root.clearInterval?.(levelTimer);
        source?.disconnect(); analyser?.disconnect();
        if (!suppliedAudioContext) void audioContext?.close()?.catch?.(() => {});
        stream?.getTracks().forEach(track => track.stop());
        channel?.close(); peer?.close(); abort.abort();
    }
    function cancel() { cancelled = true; clean(); resolveFinish(null); }
    function fail(message) {
        if (cancelled) return;
        cancelled = true; clean(); onState('error', message); rejectFinish(new Error(message));
    }
    function settled() {
        if (finishing && finishAcknowledged && !speaking && [...items.values()].every(item => item.final)) {
            const text = order.map(id => items.get(id)?.text || '').join(' ').trim();
            cancelled = true; clean(); resolveFinish(text);
        }
    }
    function commitAudio() {
        if (!cancelled && channel?.readyState === 'open') channel.send(JSON.stringify({ type: 'input_audio_buffer.commit' }));
    }
    function monitorPauses() {
        source = audioContext.createMediaStreamSource(stream);
        analyser = audioContext.createAnalyser(); analyser.fftSize = 1024;
        source.connect(analyser);
        const samples = new Float32Array(analyser.fftSize);
        levelTimer = root.setInterval(() => {
            if (cancelled || finishing || channel?.readyState !== 'open') return;
            analyser.getFloatTimeDomainData(samples);
            let power = 0;
            for (const sample of samples) power += sample * sample;
            const level = Math.sqrt(power / samples.length);
            const now = root.performance.now();
            // A short sustained onset rejects isolated clicks. Browser capture
            // supplies echo/noise cancellation; this detects acoustic pauses,
            // not semantic completion. Keep explicit Finish/End available.
            if (level >= Math.max(0.012, noise * 3)) {
                voiceSince ??= now;
                lastVoiceAt = now;
                if (!speaking && now - voiceSince >= 120) {
                    speaking = true; onSpeechStart?.(); onEvent('speech_started');
                }
            } else {
                voiceSince = null;
                if (!speaking) noise = noise * 0.98 + Math.min(level, 0.012) * 0.02;
                if (speaking && now - lastVoiceAt >= 900) {
                    speaking = false; onEvent('speech_stopped'); commitAudio();
                }
            }
        }, 30);
    }
    function receive(event) {
        if (cancelled) return;
        const id = event.item_id;
        if (event.type === 'error') {
            if (event.error?.code === 'input_audio_buffer_commit_empty') { speaking = false; finishAcknowledged = true; settled(); return; }
            fail('Live transcription stopped. Your completed text is preserved; try recorded dictation.'); return;
        }
        if (event.type === 'input_audio_buffer.speech_started') {
            speaking = true;
            if (id && !items.has(id)) items.set(id, { text: '', final: false });
            onSpeechStart?.(); onEvent('speech_started', { item_id: id });
        }
        if (event.type === 'input_audio_buffer.speech_stopped') {
            speaking = false; onEvent('speech_stopped', { item_id: id });
        }
        if (event.type === 'input_audio_buffer.committed' && finishing) { finishAcknowledged = true; speaking = false; }
        if (event.type === 'input_audio_buffer.committed' && id && !order.includes(id)) {
            // Commit events are ordered; completion events can arrive out of order.
            order.push(id);
            if (!items.has(id)) items.set(id, { text: '', final: false });
        }
        if (event.type === 'conversation.item.input_audio_transcription.delta' && id) {
            const item = items.get(id) || { text: '', final: false };
            if (!item.final) item.text += event.delta || '';
            items.set(id, item);
            onPartial?.(item.text);
        }
        if (event.type === 'conversation.item.input_audio_transcription.completed' && id) {
            const item = items.get(id) || {};
            if (!item.final) {
                items.set(id, { ...item, text: event.transcript || '', final: true });
                onEvent('committed', { item_id: id, text_length: (event.transcript || '').length, final: true });
            }
        }
        if (event.type === 'conversation.item.input_audio_transcription.failed') {
            fail('An utterance could not be transcribed. Completed text is preserved.'); return;
        }
        while (emitted < order.length && items.get(order[emitted])?.final) {
            const itemId = order[emitted++];
            onFinal?.(items.get(itemId).text, itemId);
        }
        settled();
    }
    async function start() {
        if (typeof root.RTCPeerConnection !== 'function' || !root.navigator?.mediaDevices?.getUserMedia || root.isSecureContext === false) {
            fail('Live speech requires HTTPS and WebRTC. Choose recorded dictation.');
            return;
        }
        onState('requesting', 'Opening microphone…');
        try {
            audioContext ||= new (root.AudioContext || root.webkitAudioContext)();
            await audioContext.resume();
            if (cancelled) return;
            if (audioContext.state !== 'running') throw new Error('Tap the speech button to enable microphone processing.');
            stream = await root.navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
            if (cancelled) { clean(); return; }
            const settings = stream.getAudioTracks?.()[0]?.getSettings?.() || {};
            onEvent('started', { engine: 'webrtc', sample_rate: settings.sampleRate, channel_count: settings.channelCount });
            peer = new root.RTCPeerConnection();
            stream.getTracks().forEach(track => {
                peer.addTrack(track, stream);
                track.addEventListener?.('ended', () => { if (!cancelled && !finishing) fail('Microphone disconnected. Start speech again when ready.'); });
            });
            channel = peer.createDataChannel('oai-events');
            monitorPauses();
            channel.onmessage = ({ data }) => { try { receive(JSON.parse(data)); } catch (_) { fail('Invalid speech event. Reconnect to continue.'); } };
            channel.onopen = () => { root.clearTimeout?.(connectTimer); if (!cancelled) onState('listening', 'Listening…'); };
            channel.onclose = () => { if (!cancelled) fail('Speech disconnected. Completed text is preserved.'); };
            peer.onconnectionstatechange = () => {
                if (!cancelled && ['failed', 'disconnected'].includes(peer.connectionState)) fail('Speech connection lost. Reconnect to continue.');
            };
            onState('connecting', 'Connecting live transcription…');
            const offer = await peer.createOffer();
            if (cancelled) return;
            await peer.setLocalDescription(offer);
            const response = await fetchImpl('/api/speech/connection', {
                method: 'POST', credentials: 'same-origin', signal: abort.signal,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sdp: offer.sdp, context: context.text || '', vocabulary: context.vocabulary || [], language: context.language || '' })
            });
            const data = await response.json();
            if (cancelled) return;
            if (!response.ok) throw new Error(data.message || 'Live speech is unavailable. Choose recorded dictation.');
            model = data.model;
            onEvent('started', { engine: 'webrtc', model: data.model, vocabulary_count: data.vocabulary_count, context_chars: data.context_chars });
            await peer.setRemoteDescription({ type: 'answer', sdp: data.sdp });
            if (!cancelled && channel.readyState !== 'open') {
                connectTimer = root.setTimeout?.(() => fail('The audio connection could not open. Try recorded dictation.'), 30000);
            }
        } catch (error) {
            if (!cancelled) { fail(error.name === 'NotAllowedError' ? 'Microphone access was denied.' : error.message); throw error; }
        }
    }
    function finish() {
        if (cancelled || finishing) return completion;
        finishing = true;
        speaking = false;
        stream?.getTracks().forEach(track => { track.enabled = false; });
        if (channel?.readyState !== 'open') { cancel(); return completion; }
        onState('finishing', 'Finishing the current utterance…');
        commitAudio();
        // Release a paid media connection whose commit acknowledgement/final
        // never arrives. Completed text remains recoverable through onState.
        finishTimer = root.setTimeout?.(() => fail('The final transcript did not arrive. Completed text is preserved; reconnect to continue.'), 60000);
        settled();
        return completion;
    }
    function updateContext(next) {
        if (cancelled || finishing || channel?.readyState !== 'open') return;
        const prompt = String(next.text || '').slice(-6000);
        const keywords = [...new Set((next.vocabulary || []).filter(v => typeof v === 'string')
            .map(v => v.replace(/[<>\r\n]/g, ' ').trim().slice(0, 100)).filter(Boolean))].slice(0, 80);
        channel.send(JSON.stringify({ type: 'session.update', session: { type: 'transcription', audio: { input: {
            transcription: { model, prompt, keywords }
        } } } }));
        onEvent('state', { state: 'context_updated', context_chars: prompt.length, vocabulary_count: keywords.length });
    }
    return { start, finish, cancel, receive, updateContext, completedText: () => order.filter(id => items.get(id)?.final).map(id => items.get(id).text).join(' ') };
}

// PCM playback works across Web Audio browsers and starts before the HTTP stream
// is complete. Queue at most two seconds ahead so cancellation is prompt/bounded.
export function createSpeechPlayback({ fetchImpl, root = globalThis, onEvent = () => {} }) {
    let audioContext, current = null;
    function unlock() {
        if (!audioContext) audioContext = new (root.AudioContext || root.webkitAudioContext)();
        return audioContext.resume();
    }
    function stop(reason = 'interrupted') {
        const old = current; current = null;
        if (old) {
            old.abort.abort(); old.nodes.forEach(node => { try { node.stop(); } catch (_) {} });
            onEvent('playback_ended', { reason });
        }
    }
    async function speak(text) {
        stop('replaced');
        const playback = { abort: new AbortController(), nodes: new Set() };
        current = playback;
        await unlock();
        if (current !== playback) return;
        if (audioContext.state !== 'running') { stop('blocked'); throw new Error('Tap Start voice to enable audio playback.'); }
        let time = audioContext.currentTime, tail = new Uint8Array(0), started = false;
        try {
            const response = await fetchImpl('/api/speech/speak', { method: 'POST', credentials: 'same-origin', signal: playback.abort.signal,
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.message || 'Speech could not play. The answer remains in chat.');
            }
            if (!response.body?.getReader) throw new Error('Speech could not play. The answer remains in chat.');
            const reader = response.body.getReader();
            while (current === playback) {
                const { done, value } = await reader.read();
                if (done) break;
                const bytes = new Uint8Array(tail.length + value.length); bytes.set(tail); bytes.set(value, tail.length);
                const count = Math.floor(bytes.length / 2); tail = bytes.slice(count * 2);
                if (!count) continue;
                const buffer = audioContext.createBuffer(1, count, 24000);
                const samples = buffer.getChannelData(0), view = new DataView(bytes.buffer);
                for (let i = 0; i < count; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
                while (current === playback && time - audioContext.currentTime > 2) await new Promise(resolve => root.setTimeout(resolve, 50));
                if (current !== playback) break;
                const node = audioContext.createBufferSource(); node.buffer = buffer; node.connect(audioContext.destination);
                playback.nodes.add(node); node.onended = () => playback.nodes.delete(node);
                time = Math.max(time, audioContext.currentTime + 0.02); node.start(time); time += buffer.duration;
                if (!started) { started = true; onEvent('playback_started', {
                    model: response.headers?.get?.('X-Speech-Model'), format: response.headers?.get?.('X-Speech-Format')
                }); }
            }
            while (current === playback && playback.nodes.size) await new Promise(resolve => root.setTimeout(resolve, 50));
            if (current === playback) { current = null; onEvent('playback_ended', { reason: 'completed' }); }
        } catch (error) {
            if (current === playback) { stop('error'); throw error; }
        }
    }
    function dispose() { stop('ended'); void audioContext?.close(); audioContext = null; }
    return { unlock, speak, stop, dispose, getAudioContext: () => audioContext };
}
