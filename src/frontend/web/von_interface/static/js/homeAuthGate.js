const AUTH_STATUS_ENDPOINT = '/von/api/auth/status';
const AUTH_REQUEST_TIMEOUT_MS = 10000;
const AUTH_LOGIN_TIMEOUT_MS = 60000;

function getFetch(fetchImpl, windowObject) {
  if (typeof fetchImpl === 'function') return fetchImpl;
  if (typeof windowObject?.fetch === 'function') {
    return windowObject.fetch.bind(windowObject);
  }
  throw new Error('Authentication status cannot be checked in this browser.');
}

async function readJsonResponse(response) {
  if (!response?.ok) {
    throw new Error(`Authentication request failed (${response?.status || 'unknown status'}).`);
  }
  return response.json();
}

async function requestAuthJson(request, url, options, {
  windowObject,
  timeoutMs = AUTH_REQUEST_TIMEOUT_MS,
} = {}) {
  const setTimer = windowObject?.setTimeout?.bind(windowObject) || globalThis.setTimeout;
  const clearTimer = windowObject?.clearTimeout?.bind(windowObject) || globalThis.clearTimeout;
  const AbortControllerClass = windowObject?.AbortController || globalThis.AbortController;
  const controller = typeof AbortControllerClass === 'function'
    ? new AbortControllerClass()
    : null;
  let timer = null;

  const timeout = new Promise((_, reject) => {
    timer = setTimer(() => {
      const timeoutError = new Error('Authentication request timed out. Retry the status check.');
      reject(timeoutError);
      controller?.abort(timeoutError);
    }, timeoutMs);
  });

  try {
    const response = await Promise.race([
      request(url, {
        ...options,
        ...(controller ? { signal: controller.signal } : {}),
      }),
      timeout,
    ]);
    return await readJsonResponse(response);
  } finally {
    if (timer !== null) clearTimer(timer);
  }
}

export function hasAuthenticatedVonActor(authStatus) {
  return authStatus?.authenticated === true
    && typeof authStatus?.user_concept_id === 'string'
    && authStatus.user_concept_id.trim().length > 0;
}

export function shouldClearHomeIdentityMirrors(authStatus) {
  return authStatus?.authenticated === false
    || (authStatus?.authenticated === true && !hasAuthenticatedVonActor(authStatus));
}

export async function fetchHomeAuthStatus({
  fetchImpl,
  windowObject = window,
  timeoutMs = AUTH_REQUEST_TIMEOUT_MS,
} = {}) {
  const request = getFetch(fetchImpl, windowObject);
  return requestAuthJson(request, AUTH_STATUS_ENDPOINT, {
    cache: 'no-store',
    credentials: 'same-origin',
  }, { windowObject, timeoutMs });
}

function setGateMessage(documentObject, message, kind = 'info') {
  const status = documentObject.getElementById('vonAuthenticationGateStatus');
  if (!status) return;
  status.textContent = message;
  status.dataset.kind = kind;
}

function setGateBusy(documentObject, busy) {
  const gate = documentObject.getElementById('vonAuthenticationGate');
  if (gate) gate.setAttribute('aria-busy', busy ? 'true' : 'false');
  for (const id of ['vonGoogleLoginButton', 'vonBrowserTestLoginButton', 'vonAuthRetryButton']) {
    const button = documentObject.getElementById(id);
    if (button) button.disabled = !!busy;
  }
}

export function applyHomeAuthState(authStatus, { documentObject = document } = {}) {
  const authenticated = hasAuthenticatedVonActor(authStatus);
  const gate = documentObject.getElementById('vonAuthenticationGate');
  const app = documentObject.getElementById('vonAuthenticatedApp');
  const browserTestButton = documentObject.getElementById('vonBrowserTestLoginButton');
  const retryButton = documentObject.getElementById('vonAuthRetryButton');
  const body = documentObject.body;

  if (gate) {
    gate.hidden = authenticated;
    gate.setAttribute('aria-busy', 'false');
  }
  if (app) {
    app.hidden = !authenticated;
    app.setAttribute('aria-hidden', authenticated ? 'false' : 'true');
  }
  if (body) {
    body.classList.toggle('von-authenticated', authenticated);
    body.classList.toggle('von-signed-out', !authenticated);
    body.classList.remove('von-auth-pending');
  }
  if (browserTestButton) {
    browserTestButton.hidden = authenticated || authStatus?.browser_test_mode?.available !== true;
  }
  if (retryButton) retryButton.hidden = true;

  if (!authenticated) {
    setGateBusy(documentObject, false);
    setGateMessage(
      documentObject,
      authStatus?.authenticated === true
        ? 'Your login succeeded, but Von could not resolve a user identity. Sign in again or retry the status check.'
        : 'Sign in to establish your identity and continue to Von.',
      authStatus?.authenticated === true ? 'error' : 'info',
    );
  }

  return authenticated;
}

export function applyHomeAuthUnavailable(error, { documentObject = document } = {}) {
  applyHomeAuthState({ authenticated: false }, { documentObject });
  const retryButton = documentObject.getElementById('vonAuthRetryButton');
  if (retryButton) retryButton.hidden = false;
  setGateMessage(
    documentObject,
    error?.message
      ? `Von could not confirm your sign-in: ${error.message}`
      : 'Von could not confirm your sign-in. Retry before continuing.',
    'error',
  );
}

export async function exchangeGoogleAuthToken(token, {
  fetchImpl,
  windowObject = window,
  timeoutMs = AUTH_LOGIN_TIMEOUT_MS,
} = {}) {
  const cleanToken = typeof token === 'string' ? token.trim() : '';
  if (!cleanToken) return null;
  const request = getFetch(fetchImpl, windowObject);
  return requestAuthJson(request, '/von/api/auth/exchange-token', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: cleanToken }),
  }, { windowObject, timeoutMs });
}

export async function loginWithBrowserTestIdentity({
  fetchImpl,
  windowObject = window,
  timeoutMs = AUTH_LOGIN_TIMEOUT_MS,
} = {}) {
  const request = getFetch(fetchImpl, windowObject);
  return requestAuthJson(request, '/von/api/auth/browser-test-login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  }, { windowObject, timeoutMs });
}

function openGoogleLoginPopup(windowObject) {
  const width = 500;
  const height = 650;
  const left = Math.max(0, Math.round((windowObject.screen?.width || width) / 2 - width / 2));
  const top = Math.max(0, Math.round((windowObject.screen?.height || height) / 2 - height / 2));
  return windowObject.open(
    '/von/api/auth/google/login',
    'vonGoogleLogin',
    `width=${width},height=${height},left=${left},top=${top},scrollbars=yes,resizable=yes`,
  );
}

async function confirmAuthenticatedActor({ fetchImpl, windowObject }) {
  const authStatus = await fetchHomeAuthStatus({ fetchImpl, windowObject });
  if (!hasAuthenticatedVonActor(authStatus)) {
    throw new Error('The server did not confirm a signed-in Von identity.');
  }
  return authStatus;
}

function bindHomeAuthActions({
  authStatus,
  documentObject,
  windowObject,
  fetchImpl,
  onAuthenticated,
}) {
  const googleButton = documentObject.getElementById('vonGoogleLoginButton');
  const browserTestButton = documentObject.getElementById('vonBrowserTestLoginButton');
  const retryButton = documentObject.getElementById('vonAuthRetryButton');

  if (retryButton && retryButton.dataset.authBound !== 'true') {
    retryButton.dataset.authBound = 'true';
    retryButton.addEventListener('click', () => windowObject.location.reload());
  }

  if (browserTestButton) {
    browserTestButton.hidden = authStatus?.browser_test_mode?.available !== true;
    if (browserTestButton.dataset.authBound !== 'true') {
      browserTestButton.dataset.authBound = 'true';
      browserTestButton.addEventListener('click', async () => {
        setGateBusy(documentObject, true);
        setGateMessage(documentObject, 'Signing in with the browser test identity…');
        try {
          await loginWithBrowserTestIdentity({ fetchImpl, windowObject });
          const confirmed = await confirmAuthenticatedActor({ fetchImpl, windowObject });
          onAuthenticated(confirmed);
        } catch (error) {
          setGateBusy(documentObject, false);
          setGateMessage(documentObject, error?.message || 'Browser test login failed.', 'error');
        }
      });
    }
  }

  if (!googleButton || googleButton.dataset.authBound === 'true') return;
  googleButton.dataset.authBound = 'true';
  googleButton.addEventListener('click', () => {
    const popup = openGoogleLoginPopup(windowObject);
    if (!popup) {
      setGateMessage(documentObject, 'The sign-in window was blocked. Allow pop-ups and try again.', 'error');
      return;
    }

    setGateBusy(documentObject, true);
    setGateMessage(documentObject, 'Complete sign-in in the Google window…');
    let completed = false;
    let closeTimer = null;

    const cleanup = () => {
      windowObject.removeEventListener('message', handleMessage);
      if (closeTimer !== null) windowObject.clearInterval(closeTimer);
    };

    const finish = async (token = null) => {
      if (completed) return;
      completed = true;
      cleanup();
      try {
        let exchangeError = null;
        if (token) {
          try {
            await exchangeGoogleAuthToken(token, { fetchImpl, windowObject });
          } catch (error) {
            // The callback may already have established the same-origin session;
            // the canonical status read below decides whether login succeeded.
            exchangeError = error;
          }
        }
        let confirmed;
        try {
          confirmed = await confirmAuthenticatedActor({ fetchImpl, windowObject });
        } catch (statusError) {
          throw exchangeError || statusError;
        }
        onAuthenticated(confirmed);
      } catch (error) {
        completed = false;
        setGateBusy(documentObject, false);
        setGateMessage(documentObject, error?.message || 'Sign-in did not complete.', 'error');
      }
    };

    const handleMessage = (event) => {
      if (event.origin !== windowObject.location.origin) return;
      if (event.data?.type !== 'googleLoginSuccess') return;
      void finish(event.data?.authToken || null);
    };

    windowObject.addEventListener('message', handleMessage);
    closeTimer = windowObject.setInterval(() => {
      if (!popup.closed) return;
      void finish();
    }, 500);
    try { popup.focus(); } catch { }
  });
}

export async function initialiseHomeAuthentication({
  documentObject = document,
  windowObject = window,
  fetchImpl,
  onAuthenticated = () => windowObject.location.reload(),
} = {}) {
  try {
    const authStatus = await fetchHomeAuthStatus({ fetchImpl, windowObject });
    const authenticated = applyHomeAuthState(authStatus, { documentObject });
    if (!authenticated) {
      bindHomeAuthActions({
        authStatus,
        documentObject,
        windowObject,
        fetchImpl,
        onAuthenticated,
      });
    }
    return authStatus;
  } catch (error) {
    applyHomeAuthUnavailable(error, { documentObject });
    bindHomeAuthActions({
      authStatus: null,
      documentObject,
      windowObject,
      fetchImpl,
      onAuthenticated,
    });
    return null;
  }
}
