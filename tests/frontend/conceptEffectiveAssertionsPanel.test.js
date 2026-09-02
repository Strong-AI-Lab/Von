/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(),
}));

function fixture() {
    document.body.innerHTML = `
        <div id="conceptStep1_test">
            <div id="conceptEffectiveAssertionsMount_test"></div>
        </div>
    `;
}

function assertion(overrides = {}) {
    return {
        assertion_id: 'ska_personal',
        assertion_revision: 1,
        status: 'asserted',
        predicate: {
            concept_id: '#V#date_of_event',
            storage_id: '#V#date_of_event',
            display_name: 'Date of event',
        },
        object: { kind: 'text', text: 'June 2026', language: 'en-NZ' },
        source_context: {
            kind: 'personal',
            label: 'Personal — visible only to you',
        },
        provenance: {
            asserted_by_user_concept_id: '#V#actor',
            capability_name: 'upsert_scoped_assertion',
            evidence: { source: '<b>user supplied</b>' },
        },
        updated_at: '2026-08-23T09:00:00Z',
        ...overrides,
    };
}

describe('concept actor-effective assertions panel', () => {
    let warnSpy;

    beforeEach(() => {
        fixture();
        jest.resetModules();
        jest.clearAllMocks();
        warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    });

    afterEach(() => warnSpy.mockRestore());

    test('renders personal and organisation assertions with distinct context labels', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                context_view: 'actor_effective',
                items: [
                    assertion(),
                    assertion({
                        assertion_id: 'ska_org',
                        predicate: { display_name: 'Event location' },
                        object: {
                            kind: 'concept',
                            concept_id: '#V#room',
                            display_name: 'Meeting Room',
                        },
                        source_context: {
                            kind: 'organisation',
                            label: 'Organisation — Example Lab',
                        },
                    }),
                ],
                has_more: false,
                next_offset: null,
                counts_are_lower_bounds: false,
            },
        });

        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );
        const result = await ensureConceptEffectiveAssertionsPanel({
            conceptId: '#V#event',
            suffix: 'test',
        });

        expect(result).toEqual({ rendered: true, contextView: 'actor_effective' });
        expect(api.getJsonDetailed.mock.calls[0][0]).toContain(
            'argument_concept_id=%23V%23event'
        );
        const panel = document.getElementById('conceptEffectiveAssertionsPanel_test');
        expect(panel.classList.contains('hidden')).toBe(false);
        expect(panel.querySelectorAll('.concept-effective-assertion-row')).toHaveLength(2);
        expect(panel.querySelector('.concept-effective-assertion-group-title').textContent)
            .toBe('Date of event');
        expect(panel.textContent).toContain('Personal — visible only to you');
        expect(panel.textContent).toContain('Organisation — Example Lab');
        expect(panel.textContent).toContain('Meeting Room');
        expect(panel.querySelector('b')).toBeNull();
        panel.querySelector('details').open = true;
        expect(panel.textContent).toContain('<b>user supplied</b>');
        expect(panel.textContent).toContain('ska_personal');
    });

    test('keeps anonymous base-only and empty effective projections hidden', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed
            .mockResolvedValueOnce({
                data: { context_view: 'base_publication', items: [], has_more: false },
            })
            .mockResolvedValueOnce({
                data: { context_view: 'actor_effective', items: [], has_more: false },
            });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#public', suffix: 'test' });
        expect(document.getElementById('conceptEffectiveAssertionsPanel_test').classList)
            .toContain('hidden');
        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#empty', suffix: 'test' });
        expect(document.getElementById('conceptEffectiveAssertionsPanel_test').classList)
            .toContain('hidden');
    });

    test('loads the next bounded page without replacing earlier assertions', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed
            .mockResolvedValueOnce({
                data: {
                    context_view: 'actor_effective',
                    items: [assertion()],
                    has_more: true,
                    next_offset: 25,
                    counts_are_lower_bounds: true,
                },
            })
            .mockResolvedValueOnce({
                data: {
                    context_view: 'actor_effective',
                    items: [assertion({ assertion_id: 'ska_second' })],
                    has_more: false,
                    next_offset: null,
                    counts_are_lower_bounds: false,
                },
            });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#event', suffix: 'test' });
        const button = document.getElementById('conceptEffectiveAssertionsLoadMore_test');
        expect(button.classList.contains('hidden')).toBe(false);
        await button.onclick();

        expect(api.getJsonDetailed.mock.calls[1][0]).toContain('offset=25');
        expect(document.querySelectorAll('.concept-effective-assertion-row')).toHaveLength(2);
        expect(button.classList.contains('hidden')).toBe(true);
    });

    test('shows a retryable degraded state without claiming completion', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockRejectedValue(new Error('HTTP 503'));
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        const result = await ensureConceptEffectiveAssertionsPanel({
            conceptId: '#V#event',
            suffix: 'test',
        });

        expect(result.contextView).toBe('degraded');
        const panel = document.getElementById('conceptEffectiveAssertionsPanel_test');
        expect(panel.classList.contains('hidden')).toBe(false);
        expect(panel.textContent).toContain('temporarily unavailable');
        expect(document.getElementById('conceptEffectiveAssertionsLoadMore_test').textContent)
            .toBe('Retry');
    });

    test('clears stale scoped rows and reloads after an organisation switch', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed
            .mockResolvedValueOnce({
                data: {
                    context_view: 'actor_effective',
                    items: [assertion()],
                    has_more: false,
                    next_offset: null,
                },
            })
            .mockResolvedValueOnce({
                data: {
                    context_view: 'actor_effective',
                    items: [assertion({
                        assertion_id: 'ska_org',
                        source_context: {
                            kind: 'organisation',
                            label: 'Organisation — Example Lab',
                        },
                    })],
                    has_more: false,
                    next_offset: null,
                },
            });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#event', suffix: 'test' });
        const panel = document.getElementById('conceptEffectiveAssertionsPanel_test');
        expect(panel.textContent).toContain('Personal — visible only to you');

        document.dispatchEvent(new CustomEvent('orgSwitched'));
        expect(panel.classList.contains('hidden')).toBe(true);
        expect(panel.querySelectorAll('.concept-effective-assertion-row')).toHaveLength(0);
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(api.getJsonDetailed).toHaveBeenCalledTimes(2);
        expect(panel.classList.contains('hidden')).toBe(false);
        expect(panel.textContent).toContain('Organisation — Example Lab');
        expect(panel.textContent).not.toContain('Personal — visible only to you');
    });

    test('labels aboutness-only text without implying a typed relation', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                context_view: 'actor_effective',
                items: [assertion({
                    concept_relevance: {
                        kind: 'aboutness_only',
                        concept_id: '#V#event',
                        aboutness_only: true,
                    },
                })],
                has_more: false,
            },
        });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#event', suffix: 'test' });
        expect(document.body.textContent).toContain(
            'Exact text linked to this concept; no typed relation asserted.'
        );
    });

    test('labels a projected tentative typed relation as non-authority-active', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                context_view: 'actor_effective',
                items: [assertion({
                    epistemic_status: 'tentative',
                    concept_relevance: {
                        kind: 'grounded_subject',
                        concept_id: '#V#event',
                        aboutness_only: false,
                    },
                })],
                has_more: false,
            },
        });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#event', suffix: 'test' });
        expect(document.body.textContent).toContain(
            'Tentative typed relation; not confirmed/authority-active'
        );
        expect(document.querySelector('.concept-effective-assertion-epistemic-tentative'))
            .not.toBeNull();
    });

    test('renders incoming and outgoing typed relations as complete direction-aware statements', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                context_view: 'actor_effective',
                items: [
                    assertion({
                        assertion_id: 'ska_member',
                        epistemic_status: 'tentative',
                        subject: {
                            concept_id: '#V#michael_witbrock',
                            display_name: 'Michael Witbrock',
                        },
                        predicate: {
                            concept_id: '#V#memberOf',
                            storage_id: '#V#memberOf',
                            display_name: 'member of',
                        },
                        object: {
                            kind: 'concept',
                            concept_id: '#V#primary_labs',
                            display_name: 'Primary Labs',
                        },
                        human_statement: 'Michael Witbrock member of Primary Labs',
                        concept_relevance: {
                            kind: 'grounded_object',
                            concept_id: '#V#primary_labs',
                            aboutness_only: false,
                        },
                    }),
                    assertion({
                        assertion_id: 'ska_location',
                        subject: {
                            concept_id: '#V#primary_labs',
                            display_name: 'Primary Labs',
                        },
                        predicate: {
                            concept_id: '#V#locatedIn',
                            storage_id: '#V#locatedIn',
                            display_name: 'located in',
                        },
                        object: {
                            kind: 'concept',
                            concept_id: '#V#auckland',
                            display_name: 'Auckland',
                        },
                        concept_relevance: {
                            kind: 'grounded_subject',
                            concept_id: '#V#primary_labs',
                            aboutness_only: false,
                        },
                    }),
                ],
                has_more: false,
            },
        });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({
            conceptId: '#V#primary_labs',
            suffix: 'test',
        });

        const incoming = document.querySelector('[data-assertion-id="ska_member"]');
        expect(incoming.dataset.relationDirection).toBe('incoming');
        expect(incoming.querySelector('.concept-effective-assertion-human-statement').textContent)
            .toBe('Michael Witbrock member of Primary Labs');
        expect(incoming.textContent).toContain(
            'Tentative typed relation; not confirmed/authority-active'
        );
        expect(incoming.textContent).toContain('Personal — visible only to you');

        const outgoing = document.querySelector('[data-assertion-id="ska_location"]');
        expect(outgoing.dataset.relationDirection).toBe('outgoing');
        expect(outgoing.querySelector('.concept-effective-assertion-human-statement').textContent)
            .toBe('Primary Labs located in Auckland');
    });

    test('refreshes only the matching concept after a knowledge-change event', async () => {
        const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        api.getJsonDetailed.mockResolvedValue({
            data: {
                context_view: 'actor_effective',
                items: [assertion()],
                has_more: false,
            },
        });
        const { ensureConceptEffectiveAssertionsPanel } = require(
            '../../src/frontend/web/von_interface/static/js/components/conceptEffectiveAssertionsPanel.js'
        );

        await ensureConceptEffectiveAssertionsPanel({ conceptId: '#V#event', suffix: 'test' });
        document.dispatchEvent(new CustomEvent('von:conceptKnowledgeChanged', {
            detail: { conceptId: '#V#other' },
        }));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(api.getJsonDetailed).toHaveBeenCalledTimes(1);

        document.dispatchEvent(new CustomEvent('von:conceptKnowledgeChanged', {
            detail: { conceptId: '#V#event' },
        }));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(api.getJsonDetailed).toHaveBeenCalledTimes(2);
    });
});
