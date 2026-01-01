/** @jest-environment jsdom */

const dynamicTabsPath = '../../src/frontend/web/von_interface/static/js/dynamicTabs.js';

describe('Description HTML fallback plain-text extraction', () => {
    beforeEach(() => {
        jest.resetModules();
        document.body.innerHTML = '<div></div>';
    });

    test('extractDescriptionPlainText preserves paragraph boundaries', () => {
        // Import module once to ensure __extractDescriptionPlainText is exposed.
        require(dynamicTabsPath);

        expect(globalThis.__extractDescriptionPlainText).toBeInstanceOf(Function);

        const root = document.createElement('div');
        root.innerHTML = '<p>First paragraph.</p><p>Second paragraph.</p><ul><li>One</li><li>Two</li></ul>';

        const text = globalThis.__extractDescriptionPlainText(root);
        // Paragraphs and list items should not run together.
        expect(text).toContain('First paragraph.\n\nSecond paragraph.');
        expect(text).toContain('Second paragraph.\n\nOne');
        expect(text).toContain('One\n\nTwo');
    });

    test('extractDescriptionPlainText returns dataset.rawText when present', () => {
        require(dynamicTabsPath);

        const root = document.createElement('div');
        root.dataset.rawText = 'Line 1\n\nLine 2';
        root.innerHTML = '<p>Ignored</p>';

        const text = globalThis.__extractDescriptionPlainText(root);
        expect(text).toBe('Line 1\n\nLine 2');
    });
});
