const DEFAULT_MIN_HEIGHT_PX = 180;
const DEFAULT_HEIGHT_PX = 440;
const DEFAULT_MAX_HEIGHT_PX = 800;
const DEFAULT_STEP_PX = 80;
const DEFAULT_VIEWPORT_OFFSET_PX = 180;
const DEFAULT_MIN_WIDTH_PX = 320;
const DEFAULT_WIDTH_STEP_PX = 120;

function finitePositiveNumber(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function getViewportHeightPx(explicitHeight = null) {
    const parsed = Number(explicitHeight);
    if (Number.isFinite(parsed) && parsed > 0) {
        return parsed;
    }
    const liveHeight = Number(globalThis?.window?.innerHeight);
    return Number.isFinite(liveHeight) && liveHeight > 0 ? liveHeight : null;
}

export function getResizableViewportHeightBounds(options = {}) {
    const minHeightPx = finitePositiveNumber(options.minHeightPx, DEFAULT_MIN_HEIGHT_PX);
    const configuredMaxHeightPx = Math.max(
        minHeightPx,
        finitePositiveNumber(options.maxHeightPx, DEFAULT_MAX_HEIGHT_PX)
    );
    const viewportOffsetPx = Math.max(
        0,
        Number.isFinite(Number(options.viewportOffsetPx))
            ? Number(options.viewportOffsetPx)
            : DEFAULT_VIEWPORT_OFFSET_PX
    );
    const viewportHeightPx = getViewportHeightPx(options.viewportHeightPx);
    const viewportMaxHeightPx = viewportHeightPx === null
        ? configuredMaxHeightPx
        : Math.max(minHeightPx, viewportHeightPx - viewportOffsetPx);

    return {
        minHeightPx: Math.round(minHeightPx),
        maxHeightPx: Math.round(Math.min(configuredMaxHeightPx, viewportMaxHeightPx))
    };
}

export function clampResizableViewportHeightPx(requestedHeightPx, options = {}) {
    const { minHeightPx, maxHeightPx } = getResizableViewportHeightBounds(options);
    const defaultHeightPx = finitePositiveNumber(options.defaultHeightPx, DEFAULT_HEIGHT_PX);
    const requested = finitePositiveNumber(requestedHeightPx, defaultHeightPx);
    return Math.round(Math.min(maxHeightPx, Math.max(minHeightPx, requested)));
}

function getMeasuredHeightPx(viewport) {
    const rectHeight = Number(viewport?.getBoundingClientRect?.().height);
    if (Number.isFinite(rectHeight) && rectHeight > 0) {
        return rectHeight;
    }
    const clientHeight = Number(viewport?.clientHeight);
    if (Number.isFinite(clientHeight) && clientHeight > 0) {
        return clientHeight;
    }
    const inlineHeight = Number.parseFloat(viewport?.style?.height || '');
    return Number.isFinite(inlineHeight) && inlineHeight > 0 ? inlineHeight : null;
}

function getMeasuredWidthPx(viewport) {
    const rectWidth = Number(viewport?.getBoundingClientRect?.().width);
    if (Number.isFinite(rectWidth) && rectWidth > 0) {
        return rectWidth;
    }
    const clientWidth = Number(viewport?.clientWidth);
    if (Number.isFinite(clientWidth) && clientWidth > 0) {
        return clientWidth;
    }
    const inlineWidth = Number.parseFloat(viewport?.style?.width || '');
    return Number.isFinite(inlineWidth) && inlineWidth > 0 ? inlineWidth : null;
}

function getAvailableWidthPx(viewport) {
    const parentWidth = Number(viewport?.parentElement?.clientWidth);
    if (Number.isFinite(parentWidth) && parentWidth > 0) {
        return parentWidth;
    }
    const viewportWidth = Number(globalThis?.window?.innerWidth);
    return Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : null;
}

/**
 * Add bounded sizing to a stable scroll viewport.
 *
 * Native resize remains available to fine pointers. Buttons in the supplied
 * control group provide equivalent decrease/increase/reset paths for keyboard
 * and coarse-pointer users. Width is opt-in through resizeAxis: "both" and
 * resets to the responsive full-width CSS default.
 */
export function initialiseResizableViewport(viewport, controls, options = {}) {
    if (!viewport || typeof viewport.addEventListener !== 'function') {
        return null;
    }
    if (viewport.__vonResizableViewportController) {
        return viewport.__vonResizableViewportController;
    }

    const config = {
        minHeightPx: finitePositiveNumber(options.minHeightPx, DEFAULT_MIN_HEIGHT_PX),
        defaultHeightPx: finitePositiveNumber(options.defaultHeightPx, DEFAULT_HEIGHT_PX),
        maxHeightPx: finitePositiveNumber(options.maxHeightPx, DEFAULT_MAX_HEIGHT_PX),
        stepPx: finitePositiveNumber(options.stepPx, DEFAULT_STEP_PX),
        minWidthPx: finitePositiveNumber(options.minWidthPx, DEFAULT_MIN_WIDTH_PX),
        maxWidthPx: finitePositiveNumber(options.maxWidthPx, Number.POSITIVE_INFINITY),
        widthStepPx: finitePositiveNumber(options.widthStepPx, DEFAULT_WIDTH_STEP_PX),
        viewportOffsetPx: Number.isFinite(Number(options.viewportOffsetPx))
            ? Math.max(0, Number(options.viewportOffsetPx))
            : DEFAULT_VIEWPORT_OFFSET_PX
    };
    const resizeAxis = options.resizeAxis === 'both' ? 'both' : 'vertical';
    const storedHeightPx = Number.parseFloat(viewport.dataset.vonResizeHeightPx || '');
    let preferredHeightPx = Number.isFinite(storedHeightPx) && storedHeightPx > 0
        ? storedHeightPx
        : config.defaultHeightPx;
    const storedWidthPx = Number.parseFloat(viewport.dataset.vonResizeWidthPx || '');
    let preferredWidthPx = Number.isFinite(storedWidthPx) && storedWidthPx > 0
        ? storedWidthPx
        : null;
    let enabled = options.enabled !== false;
    let resizeObserver = null;
    let lastAppliedInlineHeight = viewport.style.height || '';
    let lastAppliedInlineWidth = viewport.style.width || '';

    const decreaseButton = controls?.querySelector?.('[data-resize-action="decrease"]') || null;
    const increaseButton = controls?.querySelector?.('[data-resize-action="increase"]') || null;
    const resetButton = controls?.querySelector?.('[data-resize-action="reset"]') || null;
    const decreaseWidthButton = controls?.querySelector?.('[data-resize-action="decrease-width"]') || null;
    const increaseWidthButton = controls?.querySelector?.('[data-resize-action="increase-width"]') || null;
    const resetWidthButton = controls?.querySelector?.('[data-resize-action="reset-width"]') || null;
    const buttons = [
        decreaseButton,
        increaseButton,
        resetButton,
        decreaseWidthButton,
        increaseWidthButton,
        resetWidthButton
    ].filter(Boolean);

    viewport.classList.add('von-resizable-viewport');
    viewport.dataset.resizeAxis = resizeAxis;
    if (controls) {
        controls.classList.add('von-resize-controls');
        if (viewport.id) {
            controls.setAttribute('aria-controls', viewport.id);
            buttons.forEach((button) => button.setAttribute('aria-controls', viewport.id));
        }
    }

    const boundsOptions = () => ({
        ...config,
        viewportHeightPx: getViewportHeightPx()
    });

    const getRenderedHeightPx = () => clampResizableViewportHeightPx(preferredHeightPx, boundsOptions());

    const getWidthBounds = () => {
        const availableWidthPx = getAvailableWidthPx(viewport);
        const configuredMaxWidthPx = config.maxWidthPx;
        const maxWidthPx = Math.max(
            config.minWidthPx,
            Math.min(configuredMaxWidthPx, availableWidthPx ?? configuredMaxWidthPx)
        );
        return { minWidthPx: config.minWidthPx, maxWidthPx };
    };

    const clampWidth = (requestedWidthPx) => {
        const { minWidthPx, maxWidthPx } = getWidthBounds();
        const measuredWidthPx = getMeasuredWidthPx(viewport);
        const fallbackWidthPx = measuredWidthPx ?? maxWidthPx;
        const requested = finitePositiveNumber(requestedWidthPx, fallbackWidthPx);
        return Math.round(Math.min(maxWidthPx, Math.max(minWidthPx, requested)));
    };

    const getRenderedWidthPx = () => preferredWidthPx === null
        ? null
        : clampWidth(preferredWidthPx);

    const updateControls = () => {
        const { minHeightPx, maxHeightPx } = getResizableViewportHeightBounds(boundsOptions());
        const renderedHeightPx = getRenderedHeightPx();
        if (decreaseButton) {
            decreaseButton.disabled = !enabled || renderedHeightPx <= minHeightPx;
        }
        if (increaseButton) {
            increaseButton.disabled = !enabled || renderedHeightPx >= maxHeightPx;
        }
        if (resetButton) {
            resetButton.disabled = !enabled || Math.abs(preferredHeightPx - config.defaultHeightPx) < 1;
        }
        const { minWidthPx, maxWidthPx } = getWidthBounds();
        const renderedWidthPx = getRenderedWidthPx();
        const currentWidthPx = renderedWidthPx ?? clampWidth(getMeasuredWidthPx(viewport));
        if (decreaseWidthButton) {
            decreaseWidthButton.disabled = !enabled || resizeAxis !== 'both' || currentWidthPx <= minWidthPx;
        }
        if (increaseWidthButton) {
            increaseWidthButton.disabled = !enabled || resizeAxis !== 'both' || currentWidthPx >= maxWidthPx;
        }
        if (resetWidthButton) {
            resetWidthButton.disabled = !enabled || resizeAxis !== 'both' || preferredWidthPx === null;
        }
        if (controls) {
            controls.setAttribute('aria-disabled', enabled ? 'false' : 'true');
        }
    };

    const applyPreferredHeight = ({ notify = true } = {}) => {
        const renderedHeightPx = getRenderedHeightPx();
        viewport.dataset.vonResizeHeightPx = String(Math.round(preferredHeightPx));
        if (enabled) {
            viewport.style.height = `${renderedHeightPx}px`;
            lastAppliedInlineHeight = viewport.style.height;
        }
        updateControls();
        if (notify && typeof options.onHeightChange === 'function') {
            options.onHeightChange(renderedHeightPx);
        }
        return renderedHeightPx;
    };

    const setHeight = (nextHeightPx, { notify = true } = {}) => {
        preferredHeightPx = clampResizableViewportHeightPx(nextHeightPx, boundsOptions());
        return applyPreferredHeight({ notify });
    };

    const applyPreferredWidth = ({ notify = true } = {}) => {
        if (resizeAxis !== 'both') return null;
        const renderedWidthPx = getRenderedWidthPx();
        if (preferredWidthPx === null) {
            delete viewport.dataset.vonResizeWidthPx;
            viewport.style.removeProperty('width');
        } else {
            viewport.dataset.vonResizeWidthPx = String(Math.round(preferredWidthPx));
            viewport.style.width = `${Math.round(preferredWidthPx)}px`;
        }
        lastAppliedInlineWidth = viewport.style.width;
        updateControls();
        if (notify && typeof options.onWidthChange === 'function') {
            options.onWidthChange(renderedWidthPx);
        }
        return renderedWidthPx;
    };

    const setWidth = (nextWidthPx, { notify = true } = {}) => {
        if (resizeAxis !== 'both') return null;
        preferredWidthPx = clampWidth(nextWidthPx);
        return applyPreferredWidth({ notify });
    };

    const resetWidth = () => {
        if (resizeAxis !== 'both') return null;
        preferredWidthPx = null;
        return applyPreferredWidth();
    };

    const setEnabled = (nextEnabled) => {
        enabled = nextEnabled !== false;
        viewport.classList.toggle('von-resizable-viewport-active', enabled);
        if (enabled) {
            applyPreferredHeight({ notify: false });
            applyPreferredWidth({ notify: false });
        } else {
            viewport.style.removeProperty('height');
            viewport.style.removeProperty('width');
            lastAppliedInlineHeight = '';
            lastAppliedInlineWidth = '';
            updateControls();
        }
    };

    const persistMeasuredHeight = () => {
        if (!enabled) return;
        const inlineHeight = viewport.style.height || '';
        if (!inlineHeight || inlineHeight === lastAppliedInlineHeight) return;
        const measuredHeightPx = getMeasuredHeightPx(viewport);
        if (!Number.isFinite(measuredHeightPx) || measuredHeightPx <= 0) return;
        const clampedHeightPx = clampResizableViewportHeightPx(measuredHeightPx, boundsOptions());
        if (Math.abs(clampedHeightPx - preferredHeightPx) < 1) {
            lastAppliedInlineHeight = inlineHeight;
            return;
        }
        preferredHeightPx = clampedHeightPx;
        viewport.dataset.vonResizeHeightPx = String(clampedHeightPx);
        if (Math.abs(clampedHeightPx - measuredHeightPx) >= 1) {
            viewport.style.height = `${clampedHeightPx}px`;
        }
        lastAppliedInlineHeight = viewport.style.height;
        updateControls();
        if (typeof options.onHeightChange === 'function') {
            options.onHeightChange(clampedHeightPx);
        }
    };

    const decrease = () => setHeight(getRenderedHeightPx() - config.stepPx);
    const increase = () => setHeight(getRenderedHeightPx() + config.stepPx);
    const reset = () => {
        preferredHeightPx = config.defaultHeightPx;
        return applyPreferredHeight();
    };
    const decreaseWidth = () => setWidth((getRenderedWidthPx() ?? getMeasuredWidthPx(viewport)) - config.widthStepPx);
    const increaseWidth = () => setWidth((getRenderedWidthPx() ?? getMeasuredWidthPx(viewport)) + config.widthStepPx);
    const persistMeasuredWidth = () => {
        if (!enabled || resizeAxis !== 'both') return;
        const inlineWidth = viewport.style.width || '';
        if (!inlineWidth || inlineWidth === lastAppliedInlineWidth) return;
        const inlineWidthPx = Number.parseFloat(inlineWidth);
        if (!Number.isFinite(inlineWidthPx) || inlineWidthPx <= 0) return;
        setWidth(inlineWidthPx);
    };
    const persistNativeSize = () => {
        persistMeasuredHeight();
        persistMeasuredWidth();
    };
    const onWindowResize = () => {
        if (!enabled) return;
        applyPreferredHeight({ notify: false });
        applyPreferredWidth({ notify: false });
    };

    decreaseButton?.addEventListener('click', decrease);
    increaseButton?.addEventListener('click', increase);
    resetButton?.addEventListener('click', reset);
    decreaseWidthButton?.addEventListener('click', decreaseWidth);
    increaseWidthButton?.addEventListener('click', increaseWidth);
    resetWidthButton?.addEventListener('click', resetWidth);
    ['pointerup', 'mouseup', 'touchend'].forEach((eventName) => {
        viewport.addEventListener(eventName, persistNativeSize);
    });
    globalThis?.window?.addEventListener?.('resize', onWindowResize);

    if (typeof globalThis.ResizeObserver === 'function') {
        resizeObserver = new globalThis.ResizeObserver(() => {
            persistMeasuredHeight();
            persistMeasuredWidth();
        });
        resizeObserver.observe(viewport);
    }

    const controller = {
        destroy() {
            decreaseButton?.removeEventListener('click', decrease);
            increaseButton?.removeEventListener('click', increase);
            resetButton?.removeEventListener('click', reset);
            decreaseWidthButton?.removeEventListener('click', decreaseWidth);
            increaseWidthButton?.removeEventListener('click', increaseWidth);
            resetWidthButton?.removeEventListener('click', resetWidth);
            ['pointerup', 'mouseup', 'touchend'].forEach((eventName) => {
                viewport.removeEventListener(eventName, persistNativeSize);
            });
            globalThis?.window?.removeEventListener?.('resize', onWindowResize);
            resizeObserver?.disconnect?.();
            delete viewport.__vonResizableViewportController;
        },
        getHeight: getRenderedHeightPx,
        getWidth: getRenderedWidthPx,
        reset,
        resetWidth,
        setEnabled,
        setHeight,
        setWidth
    };
    viewport.__vonResizableViewportController = controller;
    setEnabled(enabled);
    return controller;
}
