/** @jest-environment jsdom */

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';

function createBroadcastChannelHarness() {
    const channelsByName = new Map();
    return class FakeBroadcastChannel {
        constructor(name) {
            this.name = name;
            this.listeners = new Set();
            const channels = channelsByName.get(name) || new Set();
            channels.add(this);
            channelsByName.set(name, channels);
        }

        addEventListener(type, listener) {
            if (type === 'message') this.listeners.add(listener);
        }

        removeEventListener(type, listener) {
            if (type === 'message') this.listeners.delete(listener);
        }

        postMessage(data) {
            queueMicrotask(() => {
                for (const peer of channelsByName.get(this.name) || []) {
                    if (peer === this) continue;
                    for (const listener of peer.listeners) listener({ data });
                }
            });
        }

        close() {
            channelsByName.get(this.name)?.delete(this);
        }
    };
}

describe('apiService window-session request barrier', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
    });

    afterEach(() => {
        delete global.BroadcastChannel;
        delete window.BroadcastChannel;
        delete global.fetch;
    });

    test('does not fetch until a copied tab ID rotates, then sends only the replacement header', async () => {
        const FakeBroadcastChannel = createBroadcastChannelHarness();
        global.BroadcastChannel = FakeBroadcastChannel;
        window.BroadcastChannel = FakeBroadcastChannel;
        sessionStorage.setItem('von_window_session_id', 'ws_copied');

        const incumbent = new FakeBroadcastChannel('von_window_session_coordination_v1');
        incumbent.addEventListener('message', ({ data }) => {
            if (data?.type !== 'probe' || data.window_session_id !== 'ws_copied') return;
            incumbent.postMessage({
                protocol: 'von_window_session_coordination.v1',
                type: 'occupied',
                window_session_id: 'ws_copied',
                source_document_id: 'document-incumbent',
                source_priority: '0:document-incumbent',
                target_document_id: data.source_document_id,
                probe_id: data.probe_id,
            });
        });
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({ success: true }),
        }));

        const { getJson } = require(apiServicePath);
        const request = getJson('/actor-scoped');

        expect(global.fetch).not.toHaveBeenCalled();
        await expect(request).resolves.toEqual({ success: true });

        expect(global.fetch).toHaveBeenCalledTimes(1);
        const [, options] = global.fetch.mock.calls[0];
        expect(options.headers['X-Von-Window-Session']).toMatch(/^ws_/);
        expect(options.headers['X-Von-Window-Session']).not.toBe('ws_copied');
        expect(sessionStorage.getItem('von_window_session_id'))
            .toBe(options.headers['X-Von-Window-Session']);

        incumbent.close();
    });

    test('replaces a server-owned tab ID and retries only after typed actor mismatch', async () => {
        sessionStorage.setItem('von_window_session_id', 'ws_owned_by_another_actor');
        global.fetch = jest
            .fn()
            .mockResolvedValueOnce({
                ok: false,
                status: 403,
                json: async () => ({
                    error: 'window_session_actor_mismatch',
                    error_code: 'window_session_actor_mismatch',
                }),
            })
            .mockResolvedValueOnce({
                ok: true,
                status: 200,
                json: async () => ({ status: 'updated' }),
            });

        const { postJson } = require(apiServicePath);
        await expect(postJson('/von/api/session/set_organisation', {
            organisation_concept_id: null,
        })).resolves.toEqual({ status: 'updated' });

        expect(global.fetch).toHaveBeenCalledTimes(2);
        const firstWindowSessionId = global.fetch.mock.calls[0][1]
            .headers['X-Von-Window-Session'];
        const replacementWindowSessionId = global.fetch.mock.calls[1][1]
            .headers['X-Von-Window-Session'];
        expect(firstWindowSessionId).toBe('ws_owned_by_another_actor');
        expect(replacementWindowSessionId).toMatch(/^ws_/);
        expect(replacementWindowSessionId).not.toBe(firstWindowSessionId);
        expect(sessionStorage.getItem('von_window_session_id'))
            .toBe(replacementWindowSessionId);
    });

    test('does not replace or retry a membership denial', async () => {
        sessionStorage.setItem('von_window_session_id', 'ws_current_actor');
        global.fetch = jest.fn(async () => ({
            ok: false,
            status: 403,
            json: async () => ({
                error: 'organisation_membership_required',
                error_code: 'organisation_membership_required',
            }),
        }));

        const { postJson } = require(apiServicePath);
        const request = postJson('/von/api/session/set_organisation', {
            organisation_concept_id: '#V#not_my_org',
        });

        await expect(request).rejects.toMatchObject({
            message: 'organisation_membership_required',
            status: 403,
            payload: {
                error_code: 'organisation_membership_required',
            },
        });
        expect(global.fetch).toHaveBeenCalledTimes(1);
        expect(sessionStorage.getItem('von_window_session_id'))
            .toBe('ws_current_actor');
    });
});
