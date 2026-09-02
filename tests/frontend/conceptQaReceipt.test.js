/** @jest-environment jsdom */

const {
    renderConceptQaReceipt,
} = require('../../src/frontend/web/von_interface/static/js/components/conceptQaReceipt.js');

describe('concept Q&A representation receipt', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="target"></div>';
    });

    test('distinguishes transcript-only input from an admitted knowledge claim', () => {
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                exact_input: { status: 'not_admitted' },
                formalisation: { status: 'not_attempted' },
                notes: { status: 'no_update' },
            },
        });

        expect(document.body.textContent).toContain('Transcript only');
        expect(document.body.textContent).toContain('not stored as a knowledge claim');
        expect(document.body.textContent).toContain('No typed relation was asserted');
        expect(document.body.textContent).not.toMatch(/\brepresented\b/i);
    });

    test('keeps exact text, candidate formalisation and failed notes as separate outcomes', () => {
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                exact_input: { status: 'stored', assertion_id: 'ska-exact' },
                formalisation: { status: 'candidate' },
                notes: { status: 'failed' },
            },
        });

        expect(document.body.textContent).toContain('Exact text claim stored');
        expect(document.body.textContent).toContain('source wording remains visible');
        expect(document.body.textContent).toContain('Formalisation candidate');
        expect(document.body.textContent).toContain('no typed relation was asserted');
        expect(document.body.textContent).toContain('Concept notes update failed');
    });

    test('renders tentative membership as non-activating and offers idempotent confirmation', async () => {
        const onConfirm = jest.fn(async () => true);
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                formalisation: {
                    status: 'tentative',
                    assertion_id: 'tentative-member-1',
                    predicate_concept_id: '#V#memberOf',
                    confirmation: {
                        available: true,
                        method: 'POST',
                        action: '/api/concepts/x/q_and_a/sessions/y/formalisations/tentative-member-1/confirm',
                    },
                },
            },
        }, { onConfirm });

        expect(document.body.textContent).toContain('Tentative typed relation recorded');
        expect(document.body.textContent).toContain('does not activate identity or namespace');
        const button = document.querySelector('.concept-qa-receipt-confirm');
        expect(button.textContent).toBe('Confirm relation');
        button.click();
        await Promise.resolve();
        expect(onConfirm).toHaveBeenCalledWith(
            expect.objectContaining({ assertion_id: 'tentative-member-1' }),
            expect.objectContaining({ button }),
        );
    });

    test('shows a non-actionable confirmation state when the Q&A lifecycle disallows it', () => {
        const onConfirm = jest.fn();
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                formalisation: {
                    status: 'tentative',
                    assertion_id: 'tentative-terminal-1',
                    confirmation: { available: true },
                },
            },
        }, {
            onConfirm,
            confirmationState: {
                available: false,
                label: 'Unavailable — Q&A finished',
                reason: 'This concept Q&A is finished; start an active Q&A to confirm.',
            },
        });

        const button = document.querySelector('.concept-qa-receipt-confirm');
        expect(button.disabled).toBe(true);
        expect(button.textContent).toBe('Unavailable — Q&A finished');
        expect(button.title).toContain('Q&A is finished');
        button.click();
        expect(onConfirm).not.toHaveBeenCalled();
    });

    test('rechecks lifecycle state after a confirmation attempt instead of re-enabling stale UI', async () => {
        let active = true;
        const onConfirm = jest.fn(async () => {
            active = false;
            return false;
        });
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                formalisation: {
                    status: 'tentative',
                    assertion_id: 'tentative-race-1',
                    confirmation: { available: true },
                },
            },
        }, {
            onConfirm,
            getConfirmationState: () => active
                ? { available: true }
                : {
                    available: false,
                    label: 'Unavailable — Q&A cancelled',
                    reason: 'This concept Q&A is cancelled.',
                },
        });

        const button = document.querySelector('.concept-qa-receipt-confirm');
        button.click();
        await Promise.resolve();
        await Promise.resolve();

        expect(onConfirm).toHaveBeenCalledTimes(1);
        expect(button.disabled).toBe(true);
        expect(button.textContent).toBe('Unavailable — Q&A cancelled');
    });

    test('claims typed success only when canonical read-back is present', () => {
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                exact_input: { status: 'stored' },
                formalisation: { status: 'succeeded', read_back: { verified: true } },
            },
        });
        expect(document.body.textContent).toContain('Typed assertion succeeded');
        expect(document.body.textContent).toContain('exact source claim remains visible');
    });

    test('accepts canonical backend aliases without overstating outcomes', () => {
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                exact_input: { status: 'partial_or_failed' },
                formalisation: { status: 'not_applicable' },
                notes: { status: 'not_updated' },
            },
        });

        expect(document.body.textContent).toContain('Exact text claim partially stored');
        expect(document.body.textContent).toContain('Formalisation not attempted');
        expect(document.body.textContent).toContain('Concept notes unchanged');

        document.body.innerHTML = '<div id="target"></div>';
        renderConceptQaReceipt(document.getElementById('target'), {
            receipts: {
                formalisation: {
                    status: 'asserted',
                    canonical_read_back: { epistemic_status: 'asserted' },
                },
            },
        });
        expect(document.body.textContent).toContain('Typed assertion succeeded');
    });
});
