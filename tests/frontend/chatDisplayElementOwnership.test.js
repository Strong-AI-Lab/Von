/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'window-display-ownership-test')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    createVontologyCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    getCartoucheAppearanceSettings: jest.fn(() => ({})),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

const {
    __testOnly_renderDisplayElementsIntoContainer
} = require(chatTabModulePath);

function buildTablePayload(label, value) {
    return {
        columns: [
            { column_id: 'name', label: 'Name', data_type: 'text' },
            { column_id: 'status', label: 'Status', data_type: 'text' }
        ],
        rows: [
            {
                row_id: `row_${label}`,
                cells: [
                    {
                        column_id: 'name',
                        value_raw: label,
                        value_display: label,
                        value_type: 'text'
                    },
                    {
                        column_id: 'status',
                        value_raw: value,
                        value_display: value,
                        value_type: 'text'
                    }
                ]
            }
        ]
    };
}

describe('chat display-element renderer ownership', () => {
    test('keeps inline-derived tables primary while rendering explicit structured tables', () => {
        const container = document.createElement('div');
        container.dataset.renderMode = 'rendered';
        container.dataset.originalText = [
            'Before the tables.',
            '| Name | Status |',
            '| --- | --- |',
            '| Alpha | **Yes** |',
            '',
            '| Name | Status |',
            '| --- | --- |',
            '| Beta | `pending` |',
            'After the tables.'
        ].join('\n');
        container.innerHTML = [
            '<p>Before the tables.</p>',
            '<table class="inline-primary-table"><tbody><tr><td>Alpha</td><td><strong>Yes</strong></td></tr></tbody></table>',
            '<table class="inline-primary-table"><tbody><tr><td>Beta</td><td><code>pending</code></td></tr></tbody></table>',
            '<p>After the tables.</p>'
        ].join('');

        __testOnly_renderDisplayElementsIntoContainer(container, {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_text',
                        element_type: 'text_block',
                        channel: 'screen',
                        order: 10,
                        intent: 'primary_response',
                        payload: { text: container.dataset.originalText },
                        provenance: { source: 'response_text' }
                    },
                    {
                        element_id: 'screen_table_1',
                        element_type: 'table',
                        channel: 'screen',
                        order: 16,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Alpha', '**Yes**'),
                            source_span: { start_line: 2, end_line: 4 }
                        },
                        presentation: {
                            mode: 'inline_primary',
                            source_element_id: 'screen_text',
                            source_span: { start_line: 2, end_line: 4 }
                        },
                        provenance: {
                            source: 'screen_markdown_table',
                            table_index: 1
                        }
                    },
                    {
                        element_id: 'screen_table_2',
                        element_type: 'table',
                        channel: 'screen',
                        order: 17,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Beta', '`pending`'),
                            source_span: { start_line: 6, end_line: 8 }
                        },
                        provenance: {
                            source: 'screen_markdown_table',
                            table_index: 2
                        }
                    },
                    {
                        element_id: 'screen_structured_table_1',
                        element_type: 'table',
                        channel: 'screen',
                        order: 18,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Alpha', 'Yes with provenance'),
                            title: 'Explicit comparison'
                        },
                        presentation: { mode: 'augment' },
                        provenance: {
                            source: 'screen_structured_table',
                            table_index: 1
                        }
                    }
                ]
            }
        });

        expect(container.dataset.inlinePrimaryTableCount).toBe('2');
        expect(container.querySelectorAll('.inline-primary-table')).toHaveLength(2);
        expect(container.querySelectorAll('.chat-display-elements-table')).toHaveLength(1);
        expect(
            container.querySelector('.chat-display-elements-table-title').textContent
        ).toBe('Explicit comparison');
        expect(container.textContent).toContain('Before the tables.');
        expect(container.textContent).toContain('After the tables.');
        expect(container.textContent).toContain('Yes with provenance');
        expect(container.textContent).not.toContain('**Yes**');
        expect(container.textContent).not.toContain('`pending`');
    });

    test('raw view remains faithful and never appends display cards', () => {
        const originalText = [
            'Before.',
            '| Name | Status |',
            '| --- | --- |',
            '| Alpha | **Yes** |',
            'After.'
        ].join('\n');
        const container = document.createElement('div');
        container.dataset.renderMode = 'text';
        container.dataset.originalText = originalText;
        container.textContent = originalText;

        __testOnly_renderDisplayElementsIntoContainer(container, {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_table_1',
                        element_type: 'table',
                        channel: 'screen',
                        order: 16,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Alpha', '**Yes**'),
                            source_span: { start_line: 2, end_line: 4 }
                        },
                        presentation: {
                            mode: 'inline_primary',
                            source_element_id: 'screen_text',
                            source_span: { start_line: 2, end_line: 4 }
                        },
                        provenance: { source: 'screen_markdown_table' }
                    }
                ]
            }
        });

        expect(container.textContent).toBe(originalText);
        expect(container.querySelector('.chat-display-elements')).toBeNull();
    });

    test('keeps structured fallback when the declared inline source is missing', () => {
        const originalText = [
            '| Name | Status |',
            '| --- | --- |',
            '| Alpha | Yes |'
        ].join('\n');
        const container = document.createElement('div');
        container.dataset.renderMode = 'rendered';
        container.dataset.originalText = originalText;
        container.innerHTML = '<table><tbody><tr><td>Alpha</td><td>Yes</td></tr></tbody></table>';

        __testOnly_renderDisplayElementsIntoContainer(container, {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_table_1',
                        element_type: 'table',
                        channel: 'screen',
                        order: 16,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Alpha', 'Yes'),
                            source_span: { start_line: 1, end_line: 3 }
                        },
                        presentation: {
                            mode: 'inline_primary',
                            source_element_id: 'missing_text',
                            source_span: { start_line: 1, end_line: 3 }
                        },
                        provenance: { source: 'screen_markdown_table' }
                    }
                ]
            }
        });

        expect(container.dataset.inlinePrimaryTableCount).toBe('0');
        expect(container.querySelectorAll('table')).toHaveLength(2);
        expect(container.querySelector('.chat-display-elements-table')).not.toBeNull();
    });

    test('keeps all structured fallbacks when inline table rendering is incomplete', () => {
        const originalText = [
            '| Name | Status |',
            '| --- | --- |',
            '| Alpha | Yes |',
            '',
            '| Name | Status |',
            '| --- | --- |',
            '| Beta | pending |'
        ].join('\n');
        const container = document.createElement('div');
        container.dataset.renderMode = 'rendered';
        container.dataset.originalText = originalText;
        container.innerHTML = '<table><tbody><tr><td>Alpha</td><td>Yes</td></tr></tbody></table>';

        __testOnly_renderDisplayElementsIntoContainer(container, {
            display_elements: {
                schema_version: 'turn_display_elements_v1',
                elements: [
                    {
                        element_id: 'screen_text',
                        element_type: 'text_block',
                        channel: 'screen',
                        order: 10,
                        intent: 'primary_response',
                        payload: { text: originalText },
                        provenance: { source: 'response_text' }
                    },
                    {
                        element_id: 'screen_table_1',
                        element_type: 'table',
                        channel: 'screen',
                        order: 16,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Alpha', 'Yes'),
                            source_span: { start_line: 1, end_line: 3 }
                        },
                        presentation: {
                            mode: 'inline_primary',
                            source_element_id: 'screen_text',
                            source_span: { start_line: 1, end_line: 3 }
                        },
                        provenance: { source: 'screen_markdown_table' }
                    },
                    {
                        element_id: 'screen_table_2',
                        element_type: 'table',
                        channel: 'screen',
                        order: 17,
                        intent: 'structured_tabular_view',
                        payload: {
                            ...buildTablePayload('Beta', 'pending'),
                            source_span: { start_line: 5, end_line: 7 }
                        },
                        presentation: {
                            mode: 'inline_primary',
                            source_element_id: 'screen_text',
                            source_span: { start_line: 5, end_line: 7 }
                        },
                        provenance: { source: 'screen_markdown_table' }
                    }
                ]
            }
        });

        expect(container.dataset.inlinePrimaryTableCount).toBe('0');
        expect(container.querySelectorAll('.chat-display-elements-table')).toHaveLength(2);
        expect(container.textContent).toContain('Alpha');
        expect(container.textContent).toContain('Beta');
    });
});
