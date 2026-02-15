const COPY_JSON_PENDING_CLASS = 'copy-json-precopy';
const COPY_JSON_STATE_ATTR = 'data-copy-json-state';
const COPY_JSON_BOUND_ATTR = 'data-copy-json-bound';

let copyJsonObserver = null;

function normaliseLabel(text) {
    return String(text || '')
        .replace(/\s+/g, ' ')
        .trim();
}

export function isCopyJsonButton(button) {
    if (!(button instanceof HTMLButtonElement)) {
        return false;
    }
    return normaliseLabel(button.textContent) === 'Copy JSON';
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

    if (!isCopyJsonButton(button)) {
        return;
    }

    ensureCopyJsonButtonTracking(button);
    markCopyJsonButtonPending(button);
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
}
