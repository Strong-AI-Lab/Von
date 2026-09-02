/** @jest-environment jsdom */

import {
    applyHomeAuthState,
    fetchHomeAuthStatus,
    hasAuthenticatedVonActor,
    initialiseHomeAuthentication,
    shouldClearHomeIdentityMirrors,
} from '../../src/frontend/web/von_interface/static/js/homeAuthGate.js';

function renderGate() {
    document.body.className = 'von-auth-pending';
    document.body.innerHTML = `
        <main id="vonAuthenticationGate" aria-busy="true">
            <button id="vonGoogleLoginButton" disabled>Sign in with Google</button>
            <button id="vonBrowserTestLoginButton" hidden>Browser test login</button>
            <button id="vonAuthRetryButton" hidden>Retry</button>
            <p id="vonAuthenticationGateStatus"></p>
        </main>
        <div id="vonAuthenticatedApp" hidden>Private application</div>
    `;
}

function jsonResponse(payload, { ok = true, status = 200 } = {}) {
    return {
        ok,
        status,
        json: jest.fn().mockResolvedValue(payload),
    };
}

describe('home authentication gate', () => {
    beforeEach(() => {
        renderGate();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('uses the canonical same-origin auth status endpoint', async () => {
        const fetchImpl = jest.fn().mockResolvedValue(jsonResponse({ authenticated: false }));

        await expect(fetchHomeAuthStatus({ fetchImpl, windowObject: window })).resolves.toEqual({
            authenticated: false,
        });
        expect(fetchImpl).toHaveBeenCalledWith('/von/api/auth/status', expect.objectContaining({
            cache: 'no-store',
            credentials: 'same-origin',
        }));
    });

    test('requires both authentication and a resolved Von actor', () => {
        expect(hasAuthenticatedVonActor({ authenticated: true })).toBe(false);
        expect(hasAuthenticatedVonActor({
            authenticated: true,
            user_concept_id: '#V#authenticated_user',
        })).toBe(true);
    });

    test('clears browser mirrors only for a confirmed signed-out or invalid-actor status', () => {
        expect(shouldClearHomeIdentityMirrors(null)).toBe(false);
        expect(shouldClearHomeIdentityMirrors({ status_unavailable: true })).toBe(false);
        expect(shouldClearHomeIdentityMirrors({ authenticated: false })).toBe(true);
        expect(shouldClearHomeIdentityMirrors({ authenticated: true })).toBe(true);
        expect(shouldClearHomeIdentityMirrors({
            authenticated: true,
            user_concept_id: '#V#authenticated_user',
        })).toBe(false);
    });

    test('times out a status request so the blocking gate can offer retry', async () => {
        const fetchImpl = jest.fn(() => new Promise(() => {}));

        await expect(fetchHomeAuthStatus({
            fetchImpl,
            windowObject: window,
            timeoutMs: 5,
        })).rejects.toThrow('timed out');
    });

    test('signed-out state replaces the application with enabled login controls', () => {
        const authenticated = applyHomeAuthState({
            authenticated: false,
            browser_test_mode: { available: true },
        });

        expect(authenticated).toBe(false);
        expect(document.getElementById('vonAuthenticationGate').hidden).toBe(false);
        expect(document.getElementById('vonAuthenticatedApp').hidden).toBe(true);
        expect(document.getElementById('vonGoogleLoginButton').disabled).toBe(false);
        expect(document.getElementById('vonBrowserTestLoginButton').hidden).toBe(false);
        expect(document.body.classList.contains('von-signed-out')).toBe(true);
        expect(document.getElementById('vonAuthenticationGateStatus').textContent).toContain('Sign in');
    });

    test('resolved login reveals the application and removes the gate', () => {
        const authenticated = applyHomeAuthState({
            authenticated: true,
            user_concept_id: '#V#authenticated_user',
        });

        expect(authenticated).toBe(true);
        expect(document.getElementById('vonAuthenticationGate').hidden).toBe(true);
        expect(document.getElementById('vonAuthenticatedApp').hidden).toBe(false);
        expect(document.getElementById('vonAuthenticatedApp').getAttribute('aria-hidden')).toBe('false');
        expect(document.body.classList.contains('von-authenticated')).toBe(true);
    });

    test('browser-test login is followed by canonical status read-back', async () => {
        const onAuthenticated = jest.fn();
        const fetchImpl = jest.fn()
            .mockResolvedValueOnce(jsonResponse({
                authenticated: false,
                browser_test_mode: { available: true },
            }))
            .mockResolvedValueOnce(jsonResponse({ success: true, authenticated: true }))
            .mockResolvedValueOnce(jsonResponse({
                authenticated: true,
                user_concept_id: '#V#browser_test_user',
            }));

        await initialiseHomeAuthentication({
            documentObject: document,
            windowObject: window,
            fetchImpl,
            onAuthenticated,
        });
        document.getElementById('vonBrowserTestLoginButton').click();
        await new Promise(resolve => setTimeout(resolve, 0));

        expect(fetchImpl.mock.calls[1][0]).toBe('/von/api/auth/browser-test-login');
        expect(fetchImpl.mock.calls[1][1]).toMatchObject({
            method: 'POST',
            credentials: 'same-origin',
        });
        expect(fetchImpl.mock.calls[2][0]).toBe('/von/api/auth/status');
        expect(onAuthenticated).toHaveBeenCalledWith({
            authenticated: true,
            user_concept_id: '#V#browser_test_user',
        });
    });

    test('Google popup token exchange is confirmed against canonical session status', async () => {
        const popup = { closed: false, focus: jest.fn() };
        jest.spyOn(window, 'open').mockReturnValue(popup);
        const onAuthenticated = jest.fn();
        const fetchImpl = jest.fn()
            .mockResolvedValueOnce(jsonResponse({ authenticated: false }))
            .mockResolvedValueOnce(jsonResponse({ success: true, authenticated: true }))
            .mockResolvedValueOnce(jsonResponse({
                authenticated: true,
                user_concept_id: '#V#google_user',
            }));

        await initialiseHomeAuthentication({
            documentObject: document,
            windowObject: window,
            fetchImpl,
            onAuthenticated,
        });
        document.getElementById('vonGoogleLoginButton').click();
        window.dispatchEvent(new MessageEvent('message', {
            origin: window.location.origin,
            data: { type: 'googleLoginSuccess', authToken: 'temporary-token' },
        }));
        await new Promise(resolve => setTimeout(resolve, 0));

        expect(window.open).toHaveBeenCalledWith(
            '/von/api/auth/google/login',
            'vonGoogleLogin',
            expect.stringContaining('resizable=yes'),
        );
        expect(fetchImpl.mock.calls[1][0]).toBe('/von/api/auth/exchange-token');
        expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual({ token: 'temporary-token' });
        expect(fetchImpl.mock.calls[2][0]).toBe('/von/api/auth/status');
        expect(onAuthenticated).toHaveBeenCalledWith({
            authenticated: true,
            user_concept_id: '#V#google_user',
        });
    });

    test('status failure remains fail-closed and exposes retry', async () => {
        const onAuthenticated = jest.fn();
        const fetchImpl = jest.fn().mockResolvedValue(jsonResponse(
            { error: 'unavailable' },
            { ok: false, status: 503 },
        ));

        await expect(initialiseHomeAuthentication({
            documentObject: document,
            windowObject: window,
            fetchImpl,
            onAuthenticated,
        })).resolves.toBeNull();

        expect(document.getElementById('vonAuthenticatedApp').hidden).toBe(true);
        expect(document.getElementById('vonAuthRetryButton').hidden).toBe(false);
        expect(document.getElementById('vonAuthenticationGateStatus').dataset.kind).toBe('error');
        expect(onAuthenticated).not.toHaveBeenCalled();
    });
});
