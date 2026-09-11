import { setupDynamicLayout } from '../../src/frontend/web/von_interface/static/js/components/dynamicLayout.js';

// One installed shell, resized repeatedly like a browser session.
test('tracks footer and visual viewport changes without replacing the draft or resizing for pinch zoom', () => {
    document.body.innerHTML = `<header id="globalHeader"></header><div class="tab-container"></div>
        <div class="tab-content-area"><div class="tab-content"><textarea id="promptInput">Keep this draft</textarea></div></div>
        <footer class="footer-container"></footer>`;
    let narrow = true;
    window.matchMedia = jest.fn(() => ({ matches: narrow }));
    const viewport = new EventTarget();
    Object.assign(viewport, { height: 915, offsetTop: 0, scale: 1 });
    Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 915 });
    const observed = [];
    global.ResizeObserver = class {
        constructor(callback) { this.callback = callback; }
        observe(target) { observed.push({ target, callback: this.callback }); }
    };
    window.requestAnimationFrame = callback => callback();
    const footer = document.querySelector('footer');
    let footerHeight = 52;
    footer.getBoundingClientRect = () => ({ height: footerHeight });
    document.querySelector('header').getBoundingClientRect = () => ({ height: 90 });
    document.querySelector('.tab-container').getBoundingClientRect = () => ({ height: 48 });
    const input = document.querySelector('#promptInput');
    setupDynamicLayout();
    const value = name => document.documentElement.style.getPropertyValue(name);
    expect(value('--von-fixed-footer-clearance')).toBe('60px');
    viewport.height = 430;
    viewport.dispatchEvent(new Event('resize'));
    expect(value('--von-keyboard-inset')).toBe('485px');
    expect(value('--von-viewport-height')).toBe('430px');
    viewport.offsetTop = 20;
    viewport.dispatchEvent(new Event('scroll'));
    expect(value('--von-keyboard-inset')).toBe('465px');
    footerHeight = 76;
    observed.find(item => item.target === footer).callback();
    expect(value('--von-fixed-footer-clearance')).toBe('84px');
    viewport.scale = 2;
    viewport.dispatchEvent(new Event('resize'));
    expect(value('--von-keyboard-inset')).toBe('0px');
    expect(value('--von-viewport-height')).toBe('915px');
    narrow = false;
    window.dispatchEvent(new Event('resize'));
    expect(value('--von-fixed-footer-clearance')).toBe('64px');
    expect(document.querySelector('#promptInput')).toBe(input);
    expect(input.value).toBe('Keep this draft');
});
