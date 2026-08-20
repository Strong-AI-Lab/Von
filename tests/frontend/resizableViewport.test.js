/** @jest-environment jsdom */

import {
    clampResizableViewportHeightPx,
    getResizableViewportHeightBounds,
    initialiseResizableViewport
} from '../../src/frontend/web/von_interface/static/js/utils/resizableViewport.js';

function buildResizablePanel(id = 'panel') {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = `
        <div class="controls" role="group">
            <button type="button" data-resize-action="decrease" aria-label="Show fewer rows">−</button>
            <button type="button" data-resize-action="increase" aria-label="Show more rows">+</button>
            <button type="button" data-resize-action="reset" aria-label="Reset height">Reset</button>
        </div>
        <div id="${id}"></div>
    `;
    document.body.appendChild(wrapper);
    return {
        controls: wrapper.querySelector('.controls'),
        viewport: wrapper.querySelector(`#${id}`)
    };
}

describe('shared resizable viewport', () => {
    const originalInnerHeight = window.innerHeight;
    const originalResizeObserver = global.ResizeObserver;

    beforeEach(() => {
        document.body.innerHTML = '';
        Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
        global.ResizeObserver = undefined;
    });

    afterAll(() => {
        Object.defineProperty(window, 'innerHeight', { configurable: true, value: originalInnerHeight });
        global.ResizeObserver = originalResizeObserver;
    });

    test('clamps requested height to panel and viewport bounds', () => {
        const options = {
            minHeightPx: 180,
            defaultHeightPx: 440,
            maxHeightPx: 800,
            viewportHeightPx: 620,
            viewportOffsetPx: 180
        };

        expect(getResizableViewportHeightBounds(options)).toEqual({
            minHeightPx: 180,
            maxHeightPx: 440
        });
        expect(clampResizableViewportHeightPx(120, options)).toBe(180);
        expect(clampResizableViewportHeightPx(360, options)).toBe(360);
        expect(clampResizableViewportHeightPx(900, options)).toBe(440);
        expect(clampResizableViewportHeightPx('invalid', options)).toBe(440);
    });

    test('provides independent keyboard and touch-operable controls with reset', () => {
        const first = buildResizablePanel('firstPanel');
        const second = buildResizablePanel('secondPanel');
        const firstController = initialiseResizableViewport(first.viewport, first.controls, {
            defaultHeightPx: 440,
            minHeightPx: 180,
            maxHeightPx: 800,
            stepPx: 80
        });
        const secondController = initialiseResizableViewport(second.viewport, second.controls, {
            defaultHeightPx: 360,
            minHeightPx: 180,
            maxHeightPx: 800,
            stepPx: 60
        });

        expect(first.viewport.style.height).toBe('440px');
        expect(second.viewport.style.height).toBe('360px');
        expect(first.controls.getAttribute('aria-controls')).toBe('firstPanel');
        first.controls.querySelector('[data-resize-action="increase"]').click();
        expect(first.viewport.style.height).toBe('520px');
        expect(second.viewport.style.height).toBe('360px');

        first.controls.querySelector('[data-resize-action="decrease"]').click();
        expect(first.viewport.style.height).toBe('440px');
        firstController.setHeight(650);
        first.controls.querySelector('[data-resize-action="reset"]').click();
        expect(first.viewport.style.height).toBe('440px');

        firstController.destroy();
        secondController.destroy();
    });

    test('is idempotent and preserves size when stable viewport children rerender', () => {
        const { controls, viewport } = buildResizablePanel('stablePanel');
        const controller = initialiseResizableViewport(viewport, controls, { defaultHeightPx: 440 });
        const repeatedController = initialiseResizableViewport(viewport, controls, { defaultHeightPx: 300 });

        expect(repeatedController).toBe(controller);
        controller.setHeight(600);
        const oldChild = document.createElement('table');
        viewport.replaceChildren(oldChild);
        const newChild = document.createElement('table');
        viewport.replaceChildren(newChild);

        expect(viewport.style.height).toBe('600px');
        expect(oldChild.isConnected).toBe(false);
        expect(newChild.isConnected).toBe(true);
        controller.destroy();
    });

    test('ignores zero hidden-tab measurements and can disable without losing preference', () => {
        const { controls, viewport } = buildResizablePanel('hiddenPanel');
        viewport.getBoundingClientRect = () => ({ height: 0 });
        Object.defineProperty(viewport, 'clientHeight', { configurable: true, value: 0 });
        const controller = initialiseResizableViewport(viewport, controls, { defaultHeightPx: 440 });

        controller.setHeight(600);
        viewport.dispatchEvent(new Event('pointerup'));
        expect(controller.getHeight()).toBe(600);

        controller.setEnabled(false);
        expect(viewport.style.height).toBe('');
        expect(controls.getAttribute('aria-disabled')).toBe('true');
        controller.setEnabled(true);
        expect(viewport.style.height).toBe('600px');
        controller.destroy();
    });

    test('retains a native fine-pointer drag measurement', () => {
        const { controls, viewport } = buildResizablePanel('nativeResizePanel');
        let measuredHeight = 440;
        viewport.getBoundingClientRect = () => ({ height: measuredHeight });
        const controller = initialiseResizableViewport(viewport, controls, { defaultHeightPx: 440 });

        measuredHeight = 610;
        viewport.style.height = '610px';
        viewport.dispatchEvent(new Event('pointerup'));

        expect(controller.getHeight()).toBe(610);
        expect(viewport.dataset.vonResizeHeightPx).toBe('610');
        controller.destroy();
    });

    test('bounds both-axis width changes and offers an accessible fit-width path', () => {
        const { controls, viewport } = buildResizablePanel('bothAxisPanel');
        controls.insertAdjacentHTML('beforeend', `
            <button type="button" data-resize-action="decrease-width">Narrower</button>
            <button type="button" data-resize-action="increase-width">Wider</button>
            <button type="button" data-resize-action="reset-width">Fit</button>
        `);
        Object.defineProperty(viewport.parentElement, 'clientWidth', { configurable: true, value: 1000 });
        let measuredWidth = 1000;
        viewport.getBoundingClientRect = () => ({ height: 440, width: measuredWidth });
        const controller = initialiseResizableViewport(viewport, controls, {
            defaultHeightPx: 440,
            minWidthPx: 320,
            widthStepPx: 120,
            resizeAxis: 'both'
        });

        expect(controller.getWidth()).toBeNull();
        controls.querySelector('[data-resize-action="decrease-width"]').click();
        expect(viewport.style.width).toBe('880px');
        expect(viewport.dataset.vonResizeWidthPx).toBe('880');

        measuredWidth = 880;
        controls.querySelector('[data-resize-action="increase-width"]').click();
        expect(viewport.style.width).toBe('1000px');
        expect(controls.querySelector('[data-resize-action="increase-width"]').disabled).toBe(true);

        controls.querySelector('[data-resize-action="reset-width"]').click();
        expect(viewport.style.width).toBe('');
        expect(viewport.dataset.vonResizeWidthPx).toBeUndefined();
        expect(controls.querySelector('[data-resize-action="reset-width"]').disabled).toBe(true);

        measuredWidth = 540;
        viewport.style.width = '540px';
        viewport.dispatchEvent(new Event('pointerup'));
        expect(controller.getWidth()).toBe(540);
        expect(viewport.dataset.vonResizeWidthPx).toBe('540');
        controller.destroy();
    });

    test('retains a preferred width through a temporary responsive clamp', () => {
        const { controls, viewport } = buildResizablePanel('responsiveWidthPanel');
        controls.insertAdjacentHTML('beforeend', `
            <button type="button" data-resize-action="decrease-width">Narrower</button>
            <button type="button" data-resize-action="increase-width">Wider</button>
            <button type="button" data-resize-action="reset-width">Fit</button>
        `);
        let availableWidth = 1000;
        Object.defineProperty(viewport.parentElement, 'clientWidth', {
            configurable: true,
            get: () => availableWidth
        });
        viewport.getBoundingClientRect = () => ({
            height: 440,
            width: Math.min(
                availableWidth,
                Number.parseFloat(viewport.style.width || '') || availableWidth
            )
        });
        const controller = initialiseResizableViewport(viewport, controls, {
            minWidthPx: 320,
            widthStepPx: 120,
            resizeAxis: 'both'
        });

        controls.querySelector('[data-resize-action="decrease-width"]').click();
        expect(viewport.style.width).toBe('880px');
        expect(viewport.dataset.vonResizeWidthPx).toBe('880');

        availableWidth = 500;
        window.dispatchEvent(new Event('resize'));
        expect(controller.getWidth()).toBe(500);
        expect(viewport.style.width).toBe('880px');
        expect(viewport.dataset.vonResizeWidthPx).toBe('880');

        availableWidth = 1000;
        window.dispatchEvent(new Event('resize'));
        expect(controller.getWidth()).toBe(880);
        expect(viewport.style.width).toBe('880px');
        controller.destroy();
    });

    test('retains a preferred height through a temporary viewport clamp', () => {
        const { controls, viewport } = buildResizablePanel('responsiveHeightPanel');
        const controller = initialiseResizableViewport(viewport, controls, {
            defaultHeightPx: 440,
            minHeightPx: 180,
            maxHeightPx: 800,
            viewportOffsetPx: 180
        });

        controller.setHeight(680);
        expect(viewport.style.height).toBe('680px');
        expect(viewport.dataset.vonResizeHeightPx).toBe('680');

        Object.defineProperty(window, 'innerHeight', { configurable: true, value: 500 });
        window.dispatchEvent(new Event('resize'));
        expect(controller.getHeight()).toBe(320);
        expect(viewport.style.height).toBe('320px');
        expect(viewport.dataset.vonResizeHeightPx).toBe('680');
        expect(controls.querySelector('[data-resize-action="reset"]').disabled).toBe(false);

        Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
        window.dispatchEvent(new Event('resize'));
        expect(controller.getHeight()).toBe(680);
        expect(viewport.style.height).toBe('680px');

        Object.defineProperty(window, 'innerHeight', { configurable: true, value: 500 });
        window.dispatchEvent(new Event('resize'));
        controls.querySelector('[data-resize-action="reset"]').click();
        expect(viewport.dataset.vonResizeHeightPx).toBe('440');
        expect(controls.querySelector('[data-resize-action="reset"]').disabled).toBe(true);
        Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
        window.dispatchEvent(new Event('resize'));
        expect(controller.getHeight()).toBe(440);
        expect(viewport.style.height).toBe('440px');
        controller.destroy();
    });

    test('observes a native size drag even when pointerup is not delivered to the viewport', () => {
        const { controls, viewport } = buildResizablePanel('observedWidthPanel');
        controls.insertAdjacentHTML('beforeend', `
            <button type="button" data-resize-action="decrease-width">Narrower</button>
            <button type="button" data-resize-action="increase-width">Wider</button>
            <button type="button" data-resize-action="reset-width">Fit</button>
        `);
        Object.defineProperty(viewport.parentElement, 'clientWidth', { configurable: true, value: 1000 });
        let measuredHeight = 440;
        let measuredWidth = 1000;
        viewport.getBoundingClientRect = () => ({ height: measuredHeight, width: measuredWidth });
        let observerCallback = null;
        global.ResizeObserver = class {
            constructor(callback) {
                observerCallback = callback;
            }
            observe() {}
            disconnect() {}
        };
        const controller = initialiseResizableViewport(viewport, controls, { resizeAxis: 'both' });

        measuredHeight = 560;
        measuredWidth = 620;
        viewport.style.height = '560px';
        viewport.style.width = '620px';
        observerCallback();

        expect(controller.getHeight()).toBe(560);
        expect(viewport.dataset.vonResizeHeightPx).toBe('560');
        expect(controller.getWidth()).toBe(620);
        expect(viewport.dataset.vonResizeWidthPx).toBe('620');
        expect(controls.querySelector('[data-resize-action="reset-width"]').disabled).toBe(false);
        controller.destroy();
        global.ResizeObserver = undefined;
    });
});
