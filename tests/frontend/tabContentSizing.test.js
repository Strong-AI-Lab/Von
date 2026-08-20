/** @jest-environment jsdom */

const fs = require('fs');
const path = require('path');

const styles = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css'),
    'utf8'
);
const conceptTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/concept_tab.html'),
    'utf8'
);
const vontologyTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/vontology_tab.html'),
    'utf8'
);

function cssRule(selector) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return styles.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`, 'm'))?.[1] || '';
}

function parseTemplate(markup) {
    const template = document.createElement('template');
    template.innerHTML = markup;
    return template.content;
}

describe('full-width tab work surfaces', () => {
    test('removes accidental outer caps while preserving opt-in concept sizing', () => {
        const tabRule = cssRule('.tab-content');
        const vontologyRule = cssRule('#vontologyTab');
        const treeRule = cssRule('#vontologyTreeContainer');
        const contentWrapperRule = cssRule('.content-wrapper');
        const annotationRule = cssRule('#annotationTab > .content-wrapper.annotation-layout');
        const conceptRule = cssRule('.tab-content.dynamic-concept-tab');

        expect(tabRule).toContain('max-width: none');
        expect(tabRule).not.toContain('max-width: 900px');
        expect(tabRule).not.toContain('resize:');
        expect(vontologyRule).toContain('max-width: none');
        expect(vontologyRule).not.toContain('max-width: 800px');
        expect(treeRule).toContain('max-width: 100%');
        expect(contentWrapperRule).toContain('max-width: 800px');
        expect(annotationRule).toContain('max-width: none');
        expect(annotationRule).toContain('width: 100%');
        expect(conceptRule).toContain('--von-concept-page-width');
    });

    test('retains specialised full-width and resize behaviour outside the shared panel contract', () => {
        expect(styles).toContain('#chatTab.tab-content,\n#messagesTab.tab-content');
        expect(cssRule('#chatTab>.content-wrapper')).toContain('max-width: 1600px');
        expect(cssRule('#globalTasksTab.tab-content')).toContain('max-width: none');
        expect(cssRule('.settings-iframe')).toContain('resize: vertical');
        expect(styles).toContain('.thinking-card-wrapper.has-tools.is-expanded .thinking-card-body');
        expect(styles).toContain('resize: vertical');
    });

    test('uses responsive padding and full width on narrow tabs', () => {
        expect(styles).toMatch(/@media \(max-width: 760px\)[\s\S]*?\.tab-content-area\s*\{[\s\S]*?padding-left: 8px;/);
        expect(styles).toContain('.tab-content.dynamic-concept-tab {\n        width: 100% !important;');
        expect(styles).toMatch(/@media \(max-width: 1100px\)[\s\S]*?\.annotation-left,\s*\.annotation-right\s*\{[\s\S]*?min-width: 0;[\s\S]*?width: 100%;/);
    });

    test('contains legacy description actions inside the wider concept surface', () => {
        const wrapperRule = cssRule('.dynamic-concept-tab .type-description-wrapper');
        const actionsRule = cssRule('.dynamic-concept-tab .type-description-wrapper > .desc-actions');

        expect(wrapperRule).toContain('padding-right: 172px');
        expect(actionsRule).toContain('left: auto');
        expect(actionsRule).toContain('right: 0');
        expect(actionsRule).toContain('margin-left: 0');
    });
});

describe('opt-in dense panel sizing contract', () => {
    test('mounts accessible controls on stable Relation Extent and Vontology owners', () => {
        const conceptFragment = parseTemplate(conceptTemplate);
        const relationViewport = conceptFragment.querySelector('#relationshipsContent');
        const relationComposer = conceptFragment.querySelector('#relationshipsAddForm');
        const relationControls = conceptFragment.querySelector('#relationshipExtentResizeControls');
        const pageControls = conceptFragment.querySelector('.concept-page-resize-controls');
        const vontologyFragment = parseTemplate(vontologyTemplate);
        const treeViewport = vontologyFragment.querySelector('#vontologyTreeContainer');
        const treeControls = vontologyFragment.querySelector('#vontologyTreeResizeControls');

        expect(relationViewport.classList.contains('von-resizable-viewport')).toBe(true);
        expect(relationViewport.dataset.resizeAxis).toBe('vertical');
        expect(relationViewport.contains(relationComposer)).toBe(false);
        expect(relationViewport.nextElementSibling).toBe(relationComposer);
        expect(relationControls.querySelectorAll('button')).toHaveLength(3);
        expect(pageControls.querySelectorAll('button')).toHaveLength(3);
        expect(pageControls.querySelector('[role="separator"]')).toBeTruthy();
        expect(treeViewport.classList.contains('von-resizable-viewport')).toBe(true);
        expect(treeViewport.dataset.resizeAxis).toBe('both');
        expect(treeControls.querySelectorAll('button')).toHaveLength(6);
        expect(treeControls.querySelector('[data-resize-action="decrease-width"]')).toBeTruthy();
        expect(treeControls.querySelector('[data-resize-action="increase-width"]')).toBeTruthy();
        expect(treeControls.querySelector('[data-resize-action="reset-width"]')).toBeTruthy();
    });

    test('keeps table scrolling on the stable viewport and its headings sticky', () => {
        const innerTableWrapperRule = cssRule('.relationship-extent-viewport > .predicate-extent-table-container');
        const stickyHeaderRule = cssRule('.relationship-extent-viewport .predicate-extent-table th');
        const sharedViewportRule = cssRule('.von-resizable-viewport');

        expect(sharedViewportRule).toContain('overflow: auto');
        expect(innerTableWrapperRule).toContain('overflow: visible');
        expect(stickyHeaderRule).toContain('position: sticky');
        expect(stickyHeaderRule).toContain('top: 0');
    });

    test('restricts native resize handles to opted-in active panels', () => {
        expect(styles).toContain('.von-resizable-viewport.von-resizable-viewport-active[data-resize-axis="vertical"]');
        expect(styles).toContain('.von-resizable-viewport.von-resizable-viewport-active[data-resize-axis="both"]');
        expect(styles).toContain('@media (pointer: coarse), (max-width: 760px)');
        expect(cssRule('textarea')).toContain('resize: vertical');
    });
});
