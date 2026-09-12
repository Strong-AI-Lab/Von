import { setupDynamicLayout } from '../../src/frontend/web/von_interface/static/js/components/dynamicLayout.js';

// One installed shell, resized repeatedly like a browser session.
test('tracks footer and visual viewport changes without replacing the draft or resizing for pinch zoom', () => {
    document.body.innerHTML = `<header id="globalHeader"></header><div class="tab-container"></div>
        <div class="tab-content-area"><div class="tab-content"><textarea id="promptInput">Keep this draft</textarea></div></div>
        <footer class="footer-container"><p id="modelInfoFooter"></p><p id="serverUptimeFooter">Uptime</p></footer>`;
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
    expect(value('--von-fixed-footer-clearance')).toBe('0px');
    const diagnostics = document.querySelector('#serverUptimeFooter');
    expect(footer.parentElement.className).toBe('mobile-footer-details-panel');
    expect(diagnostics.parentElement).toBe(footer);
    viewport.height = 430;
    viewport.dispatchEvent(new Event('resize'));
    expect(value('--von-keyboard-inset')).toBe('485px');
    expect(value('--von-viewport-height')).toBe('430px');
    viewport.offsetTop = 20;
    viewport.dispatchEvent(new Event('scroll'));
    expect(value('--von-keyboard-inset')).toBe('465px');
    footerHeight = 76;
    observed.find(item => item.target === footer).callback();
    expect(value('--von-fixed-footer-clearance')).toBe('0px');
    viewport.scale = 2;
    viewport.dispatchEvent(new Event('resize'));
    expect(value('--von-keyboard-inset')).toBe('0px');
    expect(value('--von-viewport-height')).toBe('915px');
    narrow = false;
    window.dispatchEvent(new Event('resize'));
    expect(value('--von-fixed-footer-clearance')).toBe('76px');
    expect(diagnostics.parentElement).toBe(footer);
    expect(document.querySelector('.mobile-footer-details')).toBeNull();
    expect(document.querySelector('#promptInput')).toBe(input);
    expect(input.value).toBe('Keep this draft');
});

test('keeps refreshed controls unique and restores their desktop order', async () => {
    document.body.innerHTML = `<header id="globalHeader"></header><div class="tab-container"></div>
        <footer class="footer-container"><div id="modelInfoFooter"><span id="model">Model</span>
        <span class="footer-org-switcher"><button class="footer-org-current-button" data-concept-name="Full organisation">SAIL</button><details><summary class="footer-org-menu-trigger">▾</summary></details></span>
        <span id="cost">Cost</span></div><p id="uptime">Uptime</p></footer>`;
    let narrow = true;
    window.matchMedia = () => ({ matches: narrow });
    const footer = document.querySelector('footer');
    const modelInfo = document.querySelector('#modelInfoFooter');
    setupDynamicLayout();
    const oldOrg = document.querySelector('.footer-org-switcher');
    expect(oldOrg.parentElement.className).toBe('mobile-shell-controls');
    expect(document.querySelector('.mobile-org-label').textContent).toBe('SAIL');
    const replacement = oldOrg.cloneNode(true);
    replacement.querySelector('.footer-org-current-button').textContent = 'Personal';
    modelInfo.replaceChildren(document.querySelector('#model'), replacement, document.querySelector('#cost'));
    await Promise.resolve();
    await Promise.resolve();
    expect(document.querySelectorAll('.footer-org-switcher')).toHaveLength(1);
    expect(oldOrg.isConnected).toBe(false);
    expect(replacement.parentElement.className).toBe('mobile-shell-controls');
    expect(document.querySelector('.mobile-org-label').textContent).toBe('Personal');
    narrow = false;
    window.dispatchEvent(new Event('resize'));
    expect([...modelInfo.children].map(node => node.id || node.className)).toEqual(['model', 'footer-org-switcher', 'cost']);
    expect(footer.parentElement).toBe(document.body);
    expect([...footer.children].map(node => node.id)).toEqual(['modelInfoFooter', 'uptime']);
});
