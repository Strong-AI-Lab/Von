// Lightweight toast helper. Uses existing CSS classes in styles.css (.toast, .toast-info, .toast-error).

export function showToast(message, variant = 'info', opts = {}) {
    const text = String(message ?? '').trim();
    if (!text) return;

    const v = (variant || 'info').toString().toLowerCase();
    const ms = Number.isFinite(opts.durationMs) ? Number(opts.durationMs) : 4000;

    const el = document.createElement('div');
    el.className = `toast ${v === 'error' ? 'toast-error' : 'toast-info'}`;
    el.textContent = text;

    try {
        document.body.appendChild(el);
    } catch (_) {
        return;
    }

    setTimeout(() => {
        try {
            el.style.opacity = '0';
        } catch (_) { /* no-op */ }
        setTimeout(() => {
            try { el.remove(); } catch (_) { /* no-op */ }
        }, 350);
    }, ms);
}
