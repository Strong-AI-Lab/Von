const COPY_JSON_PENDING_CLASS = 'copy-json-precopy';
const COPY_JSON_SUCCESS_CLASS = 'copy-json-copied';
const COPY_JSON_ERROR_CLASS = 'copy-json-copy-failed';
const COPY_JSON_STATE_ATTR = 'data-copy-json-state';
const COPY_JSON_BOUND_ATTR = 'data-copy-json-bound';
const COPY_JSON_ROLE_ATTR = 'data-copy-json-role';
const COPY_JSON_ORIGINAL_LABEL_ATTR = 'data-copy-json-original-label';
const COPY_JSON_ORIGINAL_TITLE_ATTR = 'data-copy-json-original-title';
const COPY_JSON_ORIGINAL_ARIA_LABEL_ATTR = 'data-copy-json-original-aria-label';
const COPY_JSON_DEFAULT_LABEL = 'Copy JSON';

let copyJsonObserver = null;
const copyJsonResetTimers = new Map();

function normaliseLabel(text) {
    return String(text || '')
        .replace(/\s+/g, ' ')
        .trim();
}

export function isCopyJsonButton(button) {
    if (!(button instanceof HTMLButtonElement)) {
        return false;
    }
    const role = button.getAttribute(COPY_JSON_ROLE_ATTR);
    if (role === 'copy-json') {
        return true;
    }
    return normaliseLabel(button.textContent) === COPY_JSON_DEFAULT_LABEL;
}

function rememberCopyJsonButtonOriginals(button) {
    if (!(button instanceof HTMLButtonElement)) {
        return;
    }
    if (!button.getAttribute(COPY_JSON_ORIGINAL_LABEL_ATTR)) {
        const label = normaliseLabel(button.textContent) || COPY_JSON_DEFAULT_LABEL;
        button.setAttribute(COPY_JSON_ORIGINAL_LABEL_ATTR, label);
    }
    if (!button.getAttribute(COPY_JSON_ORIGINAL_TITLE_ATTR)) {
        const title = String(button.getAttribute('title') || '').trim();
        if (title) {
            button.setAttribute(COPY_JSON_ORIGINAL_TITLE_ATTR, title);
        }
    }
    if (!button.getAttribute(COPY_JSON_ORIGINAL_ARIA_LABEL_ATTR)) {
        const ariaLabel = String(button.getAttribute('aria-label') || '').trim();
        if (ariaLabel) {
            button.setAttribute(COPY_JSON_ORIGINAL_ARIA_LABEL_ATTR, ariaLabel);
        }
    }
}

function getOriginalCopyJsonButtonLabel(button, fallbackLabel = COPY_JSON_DEFAULT_LABEL) {
    if (!(button instanceof HTMLButtonElement)) {
        return fallbackLabel;
    }
    const current = button.getAttribute(COPY_JSON_ORIGINAL_LABEL_ATTR);
    if (current && current.trim()) {
        return current.trim();
    }
    const fallback = normaliseLabel(fallbackLabel) || COPY_JSON_DEFAULT_LABEL;
    button.setAttribute(COPY_JSON_ORIGINAL_LABEL_ATTR, fallback);
    return fallback;
}

function clearCopyJsonResetTimer(button) {
    const timer = copyJsonResetTimers.get(button);
    if (timer) {
        clearTimeout(timer);
        copyJsonResetTimers.delete(button);
    }
}

function restoreCopyJsonButtonVisuals(button, fallbackLabel = COPY_JSON_DEFAULT_LABEL) {
    if (!(button instanceof HTMLButtonElement)) {
        return;
    }
    const originalLabel = getOriginalCopyJsonButtonLabel(button, fallbackLabel);
    button.textContent = originalLabel;
    button.classList.remove(COPY_JSON_SUCCESS_CLASS, COPY_JSON_ERROR_CLASS);

    const originalTitle = button.getAttribute(COPY_JSON_ORIGINAL_TITLE_ATTR);
    if (originalTitle && originalTitle.trim()) {
        button.setAttribute('title', originalTitle.trim());
    } else {
        button.setAttribute('title', originalLabel);
    }
    const originalAriaLabel = button.getAttribute(COPY_JSON_ORIGINAL_ARIA_LABEL_ATTR);
    if (originalAriaLabel && originalAriaLabel.trim()) {
        button.setAttribute('aria-label', originalAriaLabel.trim());
    } else {
        button.setAttribute('aria-label', originalLabel);
    }
}

function markCopyJsonButtonPending(button) {
    button.setAttribute(COPY_JSON_STATE_ATTR, 'pending');
    button.classList.add(COPY_JSON_PENDING_CLASS);
}

function markCopyJsonButtonUsed(button) {
    button.setAttribute(COPY_JSON_STATE_ATTR, 'used');
    button.classList.remove(COPY_JSON_PENDING_CLASS);
}

function ensureCopyJsonButtonTracking(button) {
    if (!isCopyJsonButton(button)) {
        return;
    }
    button.setAttribute(COPY_JSON_ROLE_ATTR, 'copy-json');
    rememberCopyJsonButtonOriginals(button);

    if (button.getAttribute(COPY_JSON_BOUND_ATTR) !== 'true') {
        // Use capture so the visual state clears immediately on first click.
        button.addEventListener('click', () => {
            if (button.getAttribute(COPY_JSON_STATE_ATTR) !== 'used') {
                markCopyJsonButtonUsed(button);
            }
        }, { capture: true });
        button.setAttribute(COPY_JSON_BOUND_ATTR, 'true');
    }

    if (button.getAttribute(COPY_JSON_STATE_ATTR) !== 'used') {
        markCopyJsonButtonPending(button);
    }
}

function scanForCopyJsonButtons(root) {
    if (!root) {
        return;
    }

    if (root instanceof HTMLButtonElement) {
        ensureCopyJsonButtonTracking(root);
        return;
    }

    if (!(root instanceof Element) && !(root instanceof Document)) {
        return;
    }

    root.querySelectorAll('button').forEach(ensureCopyJsonButtonTracking);
}

export function resetCopyJsonButtonPreCopyState(button) {
    if (!(button instanceof HTMLButtonElement)) {
        return;
    }

    if (!isCopyJsonButton(button) && button.getAttribute(COPY_JSON_ROLE_ATTR) !== 'copy-json') {
        return;
    }

    ensureCopyJsonButtonTracking(button);
    clearCopyJsonResetTimer(button);
    restoreCopyJsonButtonVisuals(button);
    markCopyJsonButtonPending(button);
}

/**
 * Apply a consistent post-copy visual state for JSON copy buttons.
 * Success uses "✓ Copied" and failure uses "Copy failed", then restores.
 */
export function indicateCopyJsonButtonResult(button, isSuccess, options = {}) {
    if (!(button instanceof HTMLButtonElement)) {
        return;
    }

    const fallbackLabel = normaliseLabel(options.fallbackLabel) || COPY_JSON_DEFAULT_LABEL;
    const successLabel = normaliseLabel(options.successLabel) || '✓ Copied';
    const errorLabel = normaliseLabel(options.errorLabel) || 'Copy failed';
    const successResetDelayMs = Number.isFinite(options.successResetDelayMs)
        ? Math.max(250, Number(options.successResetDelayMs))
        : 2300;
    const errorResetDelayMs = Number.isFinite(options.errorResetDelayMs)
        ? Math.max(250, Number(options.errorResetDelayMs))
        : 1800;
    const resetDelay = isSuccess ? successResetDelayMs : errorResetDelayMs;

    button.setAttribute(COPY_JSON_ROLE_ATTR, 'copy-json');
    rememberCopyJsonButtonOriginals(button);
    getOriginalCopyJsonButtonLabel(button, fallbackLabel);
    markCopyJsonButtonUsed(button);
    clearCopyJsonResetTimer(button);

    button.classList.remove(COPY_JSON_SUCCESS_CLASS, COPY_JSON_ERROR_CLASS);
    if (isSuccess) {
        button.textContent = successLabel;
        button.classList.add(COPY_JSON_SUCCESS_CLASS);
        button.setAttribute('title', 'Copied to clipboard');
        button.setAttribute('aria-label', 'Copied to clipboard');
    } else {
        button.textContent = errorLabel;
        button.classList.add(COPY_JSON_ERROR_CLASS);
        button.setAttribute('title', 'Copy failed');
        button.setAttribute('aria-label', 'Copy failed');
    }

    const timer = setTimeout(() => {
        restoreCopyJsonButtonVisuals(button, fallbackLabel);
        copyJsonResetTimers.delete(button);
    }, resetDelay);
    copyJsonResetTimers.set(button, timer);
}

function copyTextFallback(text) {
    if (typeof document === 'undefined') {
        return false;
    }

    const textArea = document.createElement('textarea');
    textArea.value = String(text ?? '');
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    textArea.setAttribute('readonly', '');
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();

    let copied = false;
    try {
        if (typeof document.execCommand === 'function') {
            copied = document.execCommand('copy');
        }
    } catch (_) {
        copied = false;
    } finally {
        document.body.removeChild(textArea);
    }
    return copied;
}

export async function copyTextWithClipboardFallback(text) {
    const value = String(text ?? '');
    if (!value) {
        return false;
    }

    if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        try {
            await navigator.clipboard.writeText(value);
            return true;
        } catch (_) {
            return copyTextFallback(value);
        }
    }

    return copyTextFallback(value);
}

export async function copyJsonTextWithButtonFeedback(button, jsonText, options = {}) {
    const copied = await copyTextWithClipboardFallback(jsonText);
    if (button instanceof HTMLButtonElement) {
        indicateCopyJsonButtonResult(button, copied, options);
    }
    return copied;
}

export function initialiseCopyJsonButtonPreCopyState() {
    if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') {
        return;
    }

    const body = document.body;
    if (!body) {
        return;
    }

    scanForCopyJsonButtons(document);

    if (copyJsonObserver) {
        return;
    }

    copyJsonObserver = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                if (node instanceof Element) {
                    scanForCopyJsonButtons(node);
                }
            });
        });
    });

    copyJsonObserver.observe(body, { childList: true, subtree: true });
}

export function __testOnly_disconnectCopyJsonButtonObserver() {
    if (copyJsonObserver) {
        copyJsonObserver.disconnect();
        copyJsonObserver = null;
    }
    copyJsonResetTimers.forEach((_timer, button) => {
        clearCopyJsonResetTimer(button);
    });
}
