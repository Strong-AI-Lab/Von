/** @jest-environment jsdom */

const coordinatorPath = '../../src/frontend/web/von_interface/static/js/utils/workflowCapabilityStatusCoordinator.js';

describe('workflow capability status coordinator', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
        delete window.__vonWorkflowCapabilityIndexStatusCoordinator;
    });

    afterEach(() => {
        delete global.fetch;
        localStorage.clear();
        sessionStorage.clear();
        delete window.__vonWorkflowCapabilityIndexStatusCoordinator;
        jest.restoreAllMocks();
    });

    test('coalesces in-flight refreshes and publishes the accepted snapshot', async () => {
        const {
            getLatestWorkflowCapabilityIndexStatus,
            refreshWorkflowCapabilityIndexStatus,
            subscribeToWorkflowCapabilityIndexStatus,
        } = require(coordinatorPath);
        let resolveFetch;
        global.fetch = jest.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
        const seen = [];
        const unsubscribe = subscribeToWorkflowCapabilityIndexStatus((status) => seen.push(status));

        const first = refreshWorkflowCapabilityIndexStatus();
        const second = refreshWorkflowCapabilityIndexStatus();
        expect(global.fetch).toHaveBeenCalledTimes(1);

        resolveFetch({
            ok: true,
            json: async () => ({
                ready: false,
                status: 'building',
                checked_at_utc: '2026-07-14T04:00:00Z',
            }),
        });

        await expect(first).resolves.toMatchObject({ status: 'building' });
        await expect(second).resolves.toMatchObject({ status: 'building' });
        expect(getLatestWorkflowCapabilityIndexStatus()).toMatchObject({ status: 'building' });
        expect(seen).toHaveLength(1);
        unsubscribe();
    });

    test('rejects a late older snapshot after a newer ready state', () => {
        const {
            acceptWorkflowCapabilityIndexStatus,
            getLatestWorkflowCapabilityIndexStatus,
        } = require(coordinatorPath);

        acceptWorkflowCapabilityIndexStatus({
            ready: true,
            status: 'ready',
            checked_at_utc: '2026-07-14T04:00:10Z',
        });
        acceptWorkflowCapabilityIndexStatus({
            ready: false,
            status: 'building',
            checked_at_utc: '2026-07-14T04:00:00Z',
        });

        expect(getLatestWorkflowCapabilityIndexStatus()).toMatchObject({
            ready: true,
            status: 'ready',
        });
    });

    test('uses the cache-bypass endpoint for a forced refresh', async () => {
        const { refreshWorkflowCapabilityIndexStatus } = require(coordinatorPath);
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                checked_at_utc: '2026-07-14T04:00:10Z',
            }),
        }));

        await refreshWorkflowCapabilityIndexStatus({ force: true });

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/workflows/capability-index/status?nocache=1',
            expect.objectContaining({
                cache: 'no-store',
                signal: expect.any(AbortSignal),
            }),
        );
    });

    test('queues a forced refresh behind an ordinary in-flight request', async () => {
        const { refreshWorkflowCapabilityIndexStatus } = require(coordinatorPath);
        let resolveOrdinary;
        global.fetch = jest.fn((url) => {
            if (String(url).includes('nocache=1')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        ready: true,
                        status: 'ready',
                        checked_at_utc: '2026-07-14T04:00:10Z',
                    }),
                });
            }
            return new Promise((resolve) => { resolveOrdinary = resolve; });
        });

        const ordinary = refreshWorkflowCapabilityIndexStatus();
        const forced = refreshWorkflowCapabilityIndexStatus({ force: true });
        expect(global.fetch).toHaveBeenCalledTimes(1);

        resolveOrdinary({
            ok: true,
            json: async () => ({
                ready: false,
                status: 'building',
                checked_at_utc: '2026-07-14T04:00:00Z',
            }),
        });

        await expect(ordinary).resolves.toMatchObject({ status: 'building' });
        await expect(forced).resolves.toMatchObject({ status: 'ready' });
        expect(global.fetch.mock.calls.map((call) => call[0])).toEqual([
            '/api/workflows/capability-index/status',
            '/api/workflows/capability-index/status?nocache=1',
        ]);
    });

    test('releases shared coordination when the transport never settles', async () => {
        jest.useFakeTimers();
        try {
            const {
                getLatestWorkflowCapabilityIndexStatus,
                refreshWorkflowCapabilityIndexStatus,
            } = require(coordinatorPath);
            global.fetch = jest.fn(() => new Promise(() => {}));

            const first = refreshWorkflowCapabilityIndexStatus({ timeoutMs: 25 });
            await jest.advanceTimersByTimeAsync(25);

            await expect(first).resolves.toBeNull();
            expect(getLatestWorkflowCapabilityIndexStatus()).toBeNull();

            const second = refreshWorkflowCapabilityIndexStatus({ timeoutMs: 25 });
            expect(global.fetch).toHaveBeenCalledTimes(2);
            await jest.advanceTimersByTimeAsync(25);
            await expect(second).resolves.toBeNull();
        } finally {
            jest.useRealTimers();
        }
    });

    test('invalidates the shared snapshot and rejects a late response after actor change', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_a' }));
        const {
            getLatestWorkflowCapabilityIndexStatus,
            refreshWorkflowCapabilityIndexStatus,
        } = require(coordinatorPath);
        let resolveFetch;
        global.fetch = jest.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));

        const actorARefresh = refreshWorkflowCapabilityIndexStatus();
        expect(global.fetch.mock.calls[0][1].headers['X-User-Concept-ID']).toBe('#V#actor_a');

        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_b' }));
        expect(getLatestWorkflowCapabilityIndexStatus()).toBeNull();
        resolveFetch({
            ok: true,
            json: async () => ({
                ready: false,
                status: 'building',
                checked_at_utc: '2026-07-14T04:00:00Z',
            }),
        });

        await expect(actorARefresh).resolves.toBeNull();
        expect(getLatestWorkflowCapabilityIndexStatus()).toBeNull();
    });

    test('starts a separate in-flight refresh immediately for the new actor', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_a' }));
        const {
            getLatestWorkflowCapabilityIndexStatus,
            refreshWorkflowCapabilityIndexStatus,
        } = require(coordinatorPath);
        const pending = [];
        global.fetch = jest.fn(() => new Promise((resolve) => pending.push(resolve)));

        const actorARefresh = refreshWorkflowCapabilityIndexStatus();
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_b' }));
        const actorBRefresh = refreshWorkflowCapabilityIndexStatus({ force: true });

        expect(global.fetch).toHaveBeenCalledTimes(2);
        expect(global.fetch.mock.calls[0][1].headers['X-User-Concept-ID']).toBe('#V#actor_a');
        expect(global.fetch.mock.calls[1][1].headers['X-User-Concept-ID']).toBe('#V#actor_b');

        pending[1]({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Actor B capability index ready.',
                checked_at_utc: '2026-07-14T04:00:10Z',
            }),
        });
        await expect(actorBRefresh).resolves.toMatchObject({ status: 'ready' });

        pending[0]({
            ok: true,
            json: async () => ({
                ready: false,
                status: 'building',
                summary: 'Actor A capability index building.',
                checked_at_utc: '2026-07-14T04:00:20Z',
            }),
        });
        await expect(actorARefresh).resolves.toMatchObject({ status: 'ready' });
        expect(getLatestWorkflowCapabilityIndexStatus()).toMatchObject({
            status: 'ready',
            summary: 'Actor B capability index ready.',
        });
    });

    test('reset rejects an old actor response even before stored actor context changes', async () => {
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_a' }));
        const {
            getLatestWorkflowCapabilityIndexStatus,
            refreshWorkflowCapabilityIndexStatus,
            resetWorkflowCapabilityIndexStatusCoordinator,
            subscribeToWorkflowCapabilityIndexStatus,
        } = require(coordinatorPath);
        const pending = [];
        global.fetch = jest.fn(() => new Promise((resolve) => pending.push(resolve)));
        const seen = [];
        const unsubscribe = subscribeToWorkflowCapabilityIndexStatus((status) => seen.push(status));

        const actorARefresh = refreshWorkflowCapabilityIndexStatus();
        resetWorkflowCapabilityIndexStatusCoordinator();
        expect(getLatestWorkflowCapabilityIndexStatus()).toBeNull();

        pending[0]({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Stale actor A capability index ready.',
                checked_at_utc: '2026-07-14T04:00:20Z',
            }),
        });
        await expect(actorARefresh).resolves.toBeNull();
        expect(getLatestWorkflowCapabilityIndexStatus()).toBeNull();
        expect(seen).toEqual([]);

        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#actor_b' }));
        const actorBRefresh = refreshWorkflowCapabilityIndexStatus({ force: true });
        pending[1]({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                summary: 'Actor B capability index ready.',
                checked_at_utc: '2026-07-14T04:00:30Z',
            }),
        });
        await expect(actorBRefresh).resolves.toMatchObject({
            summary: 'Actor B capability index ready.',
        });
        expect(seen).toEqual([
            expect.objectContaining({ summary: 'Actor B capability index ready.' }),
        ]);
        unsubscribe();
    });

    test('prefers the per-window actor over a stale persistent actor', async () => {
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#stale_actor' }));
        sessionStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#window_actor' }));
        const { refreshWorkflowCapabilityIndexStatus } = require(coordinatorPath);
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({
                ready: true,
                status: 'ready',
                checked_at_utc: '2026-07-14T04:00:10Z',
            }),
        }));

        await refreshWorkflowCapabilityIndexStatus();

        expect(global.fetch.mock.calls[0][1].headers['X-User-Concept-ID'])
            .toBe('#V#window_actor');
    });
});
