// Tooltip suppression snippet (stopgap)
// Removes native title tooltips while preserving accessible labels.
// Rules:
// 1. Any element with a `title` and without `data-keep-title` loses the native tooltip.
// 2. If element lacks an aria-label/aria-labelledby, its former title is moved to aria-label.
// 3. Elements can opt-out by setting data-keep-title="true".
// 4. MutationObserver watches for added nodes and attribute changes (title) under document.body.
// 5. A global hook window.__VON_RESTORE_TITLES() can restore original titles if needed.

(function () {
    if (window.__VON_TOOLTIP_SUPPRESS_ACTIVE__) return; // idempotent
    window.__VON_TOOLTIP_SUPPRESS_ACTIVE__ = true;

    const ORIGINAL_TITLE_ATTR = 'data-original-title';

    function processElement(el) {
        if (!el || el.nodeType !== 1) return;
        if (el.hasAttribute('data-keep-title')) return;
        const title = el.getAttribute('title');
        if (!title) return;
        // Preserve original title if not already stored
        if (!el.hasAttribute(ORIGINAL_TITLE_ATTR)) {
            el.setAttribute(ORIGINAL_TITLE_ATTR, title);
        }
        // Promote to aria-label if no accessible name
        if (!el.hasAttribute('aria-label') && !el.hasAttribute('aria-labelledby')) {
            el.setAttribute('aria-label', title);
        }
        el.removeAttribute('title');
    }

    function walk(node) {
        if (!node) return;
        if (node.nodeType === 1) {
            processElement(node);
            const children = node.querySelectorAll('[title]');
            children.forEach(processElement);
        }
    }

    const observer = new MutationObserver(mutations => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                m.addedNodes.forEach(walk);
            } else if (m.type === 'attributes' && m.attributeName === 'title') {
                processElement(m.target);
            }
        }
    });

    function init() {
        walk(document.body);
        observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['title'] });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }

    // Expose restore utility for debugging / accessibility audits
    window.__VON_RESTORE_TITLES = function () {
        observer.disconnect();
        document.querySelectorAll('[' + ORIGINAL_TITLE_ATTR + ']').forEach(el => {
            if (!el.hasAttribute('data-keep-title')) {
                const orig = el.getAttribute(ORIGINAL_TITLE_ATTR);
                if (orig && !el.hasAttribute('title')) {
                    el.setAttribute('title', orig);
                }
            }
        });
        delete window.__VON_TOOLTIP_SUPPRESS_ACTIVE__;
    };
})();
