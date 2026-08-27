const DEFAULT_WINDOW_SESSION_KEY = 'von_window_session_id';
const DEFAULT_CHANNEL_NAME = 'von_window_session_coordination_v1';
const DEFAULT_COLLISION_PROBE_MS = 75;

function createOpaqueId(prefix, cryptoImpl, nowImpl, randomImpl) {
    const randomId = (
        cryptoImpl
        && typeof cryptoImpl.randomUUID === 'function'
    )
        ? cryptoImpl.randomUUID().replace(/-/g, '')
        : `${nowImpl().toString(36)}${randomImpl().toString(36).slice(2, 12)}`;
    return `${prefix}${randomId}`;
}

/**
 * Coordinate the opaque ID that binds one browser tab to one server-side
 * window context. Browsers copy sessionStorage into a duplicated/opener-created
 * tab, so sessionStorage alone does not guarantee uniqueness.
 *
 * The newly loading document probes same-origin tabs over BroadcastChannel.
 * An existing owner only answers; the probing document rotates its copied ID.
 * Reloads retain the tab ID even if the retiring document briefly answers.
 */
export function createWindowSessionIdentityCoordinator(options = {}) {
    const storage = options.storage
        ?? (typeof sessionStorage !== 'undefined' ? sessionStorage : null);
    const BroadcastChannelImpl = options.BroadcastChannelImpl
        ?? (
            typeof window !== 'undefined'
            && typeof window.BroadcastChannel === 'function'
                ? window.BroadcastChannel
                : null
        );
    const performanceImpl = options.performanceImpl
        ?? (typeof performance !== 'undefined' ? performance : null);
    const eventTarget = options.eventTarget
        ?? (typeof window !== 'undefined' ? window : null);
    const cryptoImpl = options.cryptoImpl
        ?? (typeof crypto !== 'undefined' ? crypto : null);
    const nowImpl = options.nowImpl || Date.now;
    const randomImpl = options.randomImpl || Math.random;
    const setTimeoutImpl = options.setTimeoutImpl || setTimeout;
    const clearTimeoutImpl = options.clearTimeoutImpl || clearTimeout;
    const storageKey = options.storageKey || DEFAULT_WINDOW_SESSION_KEY;
    const channelName = options.channelName || DEFAULT_CHANNEL_NAME;
    const collisionProbeMs = Number.isFinite(Number(options.collisionProbeMs))
        ? Math.max(0, Number(options.collisionProbeMs))
        : DEFAULT_COLLISION_PROBE_MS;
    const createWindowSessionId = options.createWindowSessionId
        || (() => createOpaqueId('ws_', cryptoImpl, nowImpl, randomImpl));
    const createDocumentInstanceId = options.createDocumentInstanceId
        || (() => createOpaqueId('document_', cryptoImpl, nowImpl, randomImpl));
    const isTopLevelDocument = options.isTopLevelDocument
        ?? (() => {
            if (typeof window === 'undefined') return true;
            try {
                return window.top === window;
            } catch {
                return false;
            }
        })();

    const documentInstanceId = createDocumentInstanceId();
    const documentPriority = String(
        options.documentPriority
        ?? `${Number(performanceImpl?.timeOrigin || nowImpl())}:${documentInstanceId}`
    );
    let inMemorySessionId = '';
    let channel = null;
    let ensurePromise = null;
    let pendingProbe = null;
    let lastSettledProbe = null;
    let established = false;
    let lateRotationPromise = null;
    let pageHideListenerBound = false;

    const dispatchIdentityChanged = (previousSessionId, settledSessionId) => {
        if (!previousSessionId || !settledSessionId || previousSessionId === settledSessionId) {
            return;
        }
        try {
            const EventConstructor = eventTarget?.CustomEvent || globalThis.CustomEvent;
            if (eventTarget?.dispatchEvent && typeof EventConstructor === 'function') {
                eventTarget.dispatchEvent(new EventConstructor(
                    'von:windowSessionIdentityChanged',
                    {
                        detail: {
                            previous_window_session_id: previousSessionId,
                            window_session_id: settledSessionId,
                        }
                    }
                ));
            }
        } catch {
            // The next actor-scoped request still uses the settled ID.
        }
    };

    const getOrCreateWindowSessionId = () => {
        let sessionId = inMemorySessionId;
        try {
            sessionId = String(storage?.getItem(storageKey) || sessionId || '').trim();
        } catch {
            sessionId = '';
        }
        if (sessionId) {
            inMemorySessionId = sessionId;
            return sessionId;
        }

        sessionId = createWindowSessionId();
        inMemorySessionId = sessionId;
        try {
            storage?.setItem(storageKey, sessionId);
        } catch {
            // Keep the in-memory value available for this request even if the
            // browser has disabled storage.
        }
        return sessionId;
    };

    const postChannelMessage = (message) => {
        try {
            channel?.postMessage(message);
        } catch {
            // BroadcastChannel is only a collision-recovery enhancement. A
            // storage or channel failure must not prevent ordinary requests.
        }
    };

    const sendOccupied = (message, currentSessionId) => {
        postChannelMessage({
            protocol: 'von_window_session_coordination.v1',
            type: 'occupied',
            window_session_id: currentSessionId,
            source_document_id: documentInstanceId,
            source_priority: documentPriority,
            target_document_id: message.source_document_id,
            probe_id: message.probe_id,
        });
    };

    const rotateLateCollision = () => {
        if (lateRotationPromise || !established) {
            return;
        }
        lateRotationPromise = (async () => {
            const previousSessionId = getOrCreateWindowSessionId();
            let replacementSessionId = createWindowSessionId();
            while (replacementSessionId === previousSessionId) {
                replacementSessionId = createWindowSessionId();
            }
            inMemorySessionId = replacementSessionId;
            try {
                storage?.setItem(storageKey, replacementSessionId);
            } catch {
                // Keep the in-memory replacement for this document.
            }
            established = false;
            ensurePromise = null;
            const settledSessionId = await ensureUniqueWindowSessionId();
            dispatchIdentityChanged(previousSessionId, settledSessionId);
        })().finally(() => {
            lateRotationPromise = null;
        });
    };

    const handleChannelMessage = (event) => {
        const message = event?.data;
        if (!message || message.protocol !== 'von_window_session_coordination.v1') {
            return;
        }
        if (message.source_document_id === documentInstanceId) {
            return;
        }

        const currentSessionId = getOrCreateWindowSessionId();
        if (
            message.type === 'probe'
            && message.window_session_id === currentSessionId
        ) {
            if (established) {
                sendOccupied(message, currentSessionId);
            } else if (pendingProbe) {
                const peerPriority = String(message.source_priority || message.source_document_id || '');
                if (documentPriority > peerPriority) {
                    pendingProbe.collisionDetected = true;
                } else {
                    sendOccupied(message, currentSessionId);
                }
            }
            return;
        }

        if (
            message.type === 'occupied'
            && pendingProbe
            && message.target_document_id === documentInstanceId
            && message.probe_id === pendingProbe.probeId
            && message.window_session_id === pendingProbe.windowSessionId
        ) {
            pendingProbe.collisionDetected = true;
            return;
        }

        if (
            message.type === 'occupied'
            && established
            && lastSettledProbe
            && message.target_document_id === documentInstanceId
            && message.probe_id === lastSettledProbe.probeId
            && message.window_session_id === currentSessionId
        ) {
            rotateLateCollision();
            return;
        }

        if (
            message.type === 'claimed'
            && message.window_session_id === currentSessionId
        ) {
            const peerPriority = String(message.source_priority || message.source_document_id || '');
            if (pendingProbe) {
                if (documentPriority > peerPriority) {
                    pendingProbe.collisionDetected = true;
                } else {
                    sendOccupied(message, currentSessionId);
                }
            } else if (established) {
                if (documentPriority > peerPriority) {
                    rotateLateCollision();
                } else {
                    sendOccupied(message, currentSessionId);
                }
            }
        }
    };

    const ensureChannel = () => {
        if (channel || !isTopLevelDocument || typeof BroadcastChannelImpl !== 'function') {
            return channel;
        }
        try {
            channel = new BroadcastChannelImpl(channelName);
            channel.addEventListener?.('message', handleChannelMessage);
            if (!channel.addEventListener) {
                channel.onmessage = handleChannelMessage;
            }
            if (!pageHideListenerBound && eventTarget?.addEventListener) {
                pageHideListenerBound = true;
                eventTarget.addEventListener('pagehide', () => {
                    pendingProbe?.settle?.({ cancelled: true });
                    try {
                        channel?.close?.();
                    } catch {
                        // Ignore teardown races.
                    }
                    channel = null;
                    established = false;
                    ensurePromise = null;
                });
                eventTarget.addEventListener('pageshow', (event) => {
                    if (event?.persisted) {
                        ensurePromise = null;
                        established = false;
                        void ensureUniqueWindowSessionId();
                    }
                });
            }
        } catch {
            channel = null;
        }
        return channel;
    };

    const probeWindowSessionId = (candidateSessionId) => new Promise((resolve) => {
        const probeId = createDocumentInstanceId();
        let settled = false;
        let timerId = null;
        const probe = {
            probeId,
            windowSessionId: candidateSessionId,
            collisionDetected: false,
            settle: ({ cancelled = false } = {}) => {
                if (settled) return;
                settled = true;
                if (timerId !== null) {
                    clearTimeoutImpl(timerId);
                }
                const collisionDetected = !cancelled
                    && probe.collisionDetected === true;
                if (pendingProbe?.probeId === probeId) {
                    pendingProbe = null;
                }
                if (!cancelled) {
                    lastSettledProbe = {
                        probeId,
                        windowSessionId: candidateSessionId,
                    };
                }
                resolve({ collisionDetected, cancelled });
            },
        };
        pendingProbe = probe;
        timerId = setTimeoutImpl(() => probe.settle(), collisionProbeMs);
        postChannelMessage({
            protocol: 'von_window_session_coordination.v1',
            type: 'probe',
            window_session_id: candidateSessionId,
            source_document_id: documentInstanceId,
            source_priority: documentPriority,
            probe_id: probeId,
        });
    });

    const ensureUniqueWindowSessionId = () => {
        if (ensurePromise) {
            return ensurePromise;
        }

        const candidateSessionId = getOrCreateWindowSessionId();
        if (!ensureChannel()) {
            ensurePromise = Promise.resolve(candidateSessionId);
            return ensurePromise;
        }

        ensurePromise = (async () => {
            let settledSessionId = candidateSessionId;
            for (let probeAttempt = 0; probeAttempt < 3; probeAttempt += 1) {
                const { collisionDetected, cancelled } = await probeWindowSessionId(settledSessionId);
                if (cancelled) {
                    return getOrCreateWindowSessionId();
                }
                if (!collisionDetected) {
                    established = true;
                    postChannelMessage({
                        protocol: 'von_window_session_coordination.v1',
                        type: 'claimed',
                        window_session_id: settledSessionId,
                        source_document_id: documentInstanceId,
                        source_priority: documentPriority,
                        probe_id: lastSettledProbe?.probeId || null,
                    });
                    dispatchIdentityChanged(candidateSessionId, settledSessionId);
                    return settledSessionId;
                }

                const collidedSessionId = settledSessionId;
                do {
                    settledSessionId = createWindowSessionId();
                } while (settledSessionId === collidedSessionId);
                inMemorySessionId = settledSessionId;
                try {
                    storage?.setItem(storageKey, settledSessionId);
                } catch {
                    // Keep the in-memory replacement for this document.
                }
            }
            established = true;
            dispatchIdentityChanged(candidateSessionId, settledSessionId);
            return settledSessionId;
        })();
        return ensurePromise;
    };

    const close = () => {
        pendingProbe?.settle?.({ cancelled: true });
        try {
            channel?.removeEventListener?.('message', handleChannelMessage);
            channel?.close?.();
        } catch {
            // Ignore teardown races.
        }
        channel = null;
        established = false;
        ensurePromise = null;
    };

    return {
        close,
        ensureUniqueWindowSessionId,
        getWindowSessionId: getOrCreateWindowSessionId,
    };
}

export {
    DEFAULT_CHANNEL_NAME as WINDOW_SESSION_COORDINATION_CHANNEL,
    DEFAULT_WINDOW_SESSION_KEY as WINDOW_SESSION_KEY,
};
