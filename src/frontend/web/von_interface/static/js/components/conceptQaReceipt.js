import { normaliseConceptQaReceipts } from '../utils/conceptQaSession.js';
import {
  normaliseReferenceManifest,
  openReferenceInspector,
} from './referenceInspector.js';

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function normaliseStatus(value) {
  return cleanText(value).toLowerCase().replace(/[\s-]+/g, '_');
}

function hasReceiptSignal(receipts) {
  return Boolean(receipts?.exact_input || receipts?.formalisation || receipts?.notes);
}

function exactInputPresentation(receipt) {
  if (!receipt) return null;
  const status = normaliseStatus(receipt.status);
  if (['not_admitted', 'transcript_only', 'question', 'meta', 'control'].includes(status)) {
    return {
      tone: 'neutral',
      label: 'Transcript only',
      text: 'This question, meta-comment or control turn was not stored as a knowledge claim.',
    };
  }
  if (['stored', 'asserted', 'succeeded', 'success'].includes(status)) {
    return {
      tone: 'success',
      label: 'Exact text claim stored',
      text: 'Stored with provenance. The source wording remains visible independently of any formalisation.',
    };
  }
  if (status === 'partial_or_failed') {
    return {
      tone: 'error',
      label: 'Exact text claim partially stored',
      text: cleanText(receipt.message)
        || 'At least one exact input storage attempt failed. The transcript remains visible; inspect the item receipts for durable claim IDs.',
    };
  }
  if (['failed', 'error', 'persistence_failed'].includes(status)) {
    return {
      tone: 'error',
      label: 'Exact text claim not stored',
      text: cleanText(receipt.message) || 'The transcript turn remains visible, but durable claim storage failed.',
    };
  }
  return status ? {
    tone: 'warning',
    label: 'Exact text claim status',
    text: cleanText(receipt.message) || status.replace(/_/g, ' '),
  } : null;
}

function isMembershipCandidate(receipt) {
  if (!receipt || typeof receipt !== 'object') return false;
  const token = [
    receipt.predicate_id,
    receipt.predicate_concept_id,
    receipt.predicate,
    receipt.relation,
    receipt.relation_kind,
  ].map(cleanText).join(' ').toLowerCase();
  return token.includes('memberof') || token.includes('member_of') || token.includes('member of');
}

function formalisationPresentation(receipt) {
  if (!receipt) return null;
  const status = normaliseStatus(receipt.status);
  if (['not_attempted', 'not_applicable', 'none', 'skipped'].includes(status)) {
    return {
      tone: 'neutral',
      label: 'Formalisation not attempted',
      text: 'No typed relation was asserted.',
    };
  }
  if (['tentative', 'uncertain'].includes(status)) {
    const membershipCandidate = isMembershipCandidate(receipt);
    return {
      tone: 'warning',
      label: 'Tentative typed relation recorded',
      text: membershipCandidate
        ? 'Recorded with clear provenance. It is not an asserted relation and does not activate identity or namespace.'
        : 'Recorded with clear provenance. It is not confirmed or authority-active.',
    };
  }
  if (['candidate', 'proposed', 'pending'].includes(status)) {
    return {
      tone: 'warning',
      label: 'Formalisation candidate',
      text: 'A candidate was recorded for review; no typed relation was asserted.',
    };
  }
  if (['deferred', 'needs_review', 'review_required'].includes(status)) {
    return {
      tone: 'warning',
      label: 'Formalisation deferred',
      text: isMembershipCandidate(receipt)
        ? 'No asserted relation was created, and identity or namespace was not activated.'
        : 'No typed relation was asserted.',
    };
  }
  if (['succeeded', 'stored', 'asserted', 'success'].includes(status)) {
    const readBack = receipt.confirmation?.last_attempt?.canonical_read_back
      ?? receipt.canonical_read_back
      ?? receipt.read_back;
    const readBackVerified = readBack === true
      || readBack?.verified === true
      || ['verified', 'succeeded', 'asserted'].includes(normaliseStatus(readBack?.status))
      || normaliseStatus(readBack?.epistemic_status) === 'asserted';
    return {
      tone: readBackVerified ? 'success' : 'warning',
      label: readBackVerified ? 'Typed assertion succeeded' : 'Typed assertion reported',
      text: readBackVerified
        ? 'The typed assertion was read back. The exact source claim remains visible above.'
        : 'The server reported a typed assertion, but canonical read-back was not included. The exact source claim remains visible.',
    };
  }
  if (['failed', 'error'].includes(status)) {
    return {
      tone: 'error',
      label: 'Formalisation failed',
      text: cleanText(receipt.message) || 'No typed relation was confirmed. The exact source claim remains available.',
    };
  }
  return status ? {
    tone: 'warning',
    label: 'Formalisation status',
    text: cleanText(receipt.message) || `${status.replace(/_/g, ' ')}; no typed relation is implied.`,
  } : null;
}

function notesPresentation(receipt) {
  if (!receipt) return null;
  const status = normaliseStatus(receipt.status);
  if (['updated', 'stored', 'succeeded', 'success'].includes(status)) {
    return { tone: 'success', label: 'Concept notes updated', text: 'The notes update completed.' };
  }
  if (['no_update', 'not_updated', 'not_attempted', 'unchanged', 'skipped'].includes(status)) {
    return { tone: 'neutral', label: 'Concept notes unchanged', text: 'No notes update was applied.' };
  }
  if (['failed', 'error', 'persistence_failed'].includes(status)) {
    return {
      tone: 'error',
      label: 'Concept notes update failed',
      text: cleanText(receipt.message) || 'This does not change the separate exact-claim status.',
    };
  }
  return status ? {
    tone: 'warning',
    label: 'Concept notes status',
    text: cleanText(receipt.message) || status.replace(/_/g, ' '),
  } : null;
}

function referenceForReceipt(receipt, manifest, preferredTypes) {
  const ids = [
    receipt?.assertion_id,
    ...(Array.isArray(receipt?.assertion_ids) ? receipt.assertion_ids : []),
    receipt?.receipt_id,
    receipt?.mutation_receipt_id,
    receipt?.reference_id,
  ].map(cleanText).filter(Boolean);
  if (!ids.length || !manifest?.references?.length) return null;
  return manifest.references.find((reference) => (
    ids.includes(cleanText(reference.reference_id))
    && (!preferredTypes.length || preferredTypes.includes(reference.reference_type))
  )) || null;
}

function normaliseConfirmationUiState(value) {
  if (!value || value.available !== false) {
    return {
      available: true,
      label: 'Confirm relation',
      reason: 'Promote this tentative relation and verify canonical read-back',
    };
  }
  return {
    available: false,
    label: cleanText(value.label) || 'Confirmation unavailable',
    reason: cleanText(value.reason)
      || 'This tentative relation cannot be confirmed from the current Q&A state.',
  };
}

function applyConfirmationUiState(button, value) {
  if (!(button instanceof HTMLButtonElement)) return;
  const state = normaliseConfirmationUiState(value);
  button.disabled = !state.available;
  button.textContent = state.label;
  button.title = state.reason;
  button.setAttribute('aria-label', state.reason);
  button.dataset.confirmationUiAvailable = state.available ? 'true' : 'false';
}

function resolveConfirmationUiState(options, receipt) {
  if (typeof options?.getConfirmationState === 'function') {
    return normaliseConfirmationUiState(options.getConfirmationState(receipt));
  }
  return normaliseConfirmationUiState(options?.confirmationState);
}

function confirmationReceiptFromButton(button) {
  return {
    confirmation: {
      available: button?.dataset?.receiptConfirmationAvailable === 'true',
      available_after_finish: button?.dataset?.availableAfterFinish === 'true',
      available_after_cancel: button?.dataset?.availableAfterCancel === 'true',
    },
  };
}

export function updateConceptQaReceiptConfirmationState(container, value) {
  if (!(container instanceof Element)) return;
  const buttons = container.matches('.concept-qa-receipt-confirm')
    ? [container]
    : Array.from(container.querySelectorAll('.concept-qa-receipt-confirm'));
  for (const button of buttons) {
    const state = typeof value === 'function'
      ? value(confirmationReceiptFromButton(button))
      : value;
    applyConfirmationUiState(button, state);
  }
}

function appendReceiptRow(
  container,
  key,
  receipt,
  presentation,
  manifest,
  onInspect,
  onConfirm,
  confirmationOptions,
) {
  if (!presentation) return;
  const row = document.createElement('div');
  row.className = `concept-qa-receipt-row tone-${presentation.tone}`;
  row.dataset.receiptKind = key;

  const copy = document.createElement('div');
  copy.className = 'concept-qa-receipt-copy';
  const label = document.createElement('strong');
  label.textContent = presentation.label;
  const detail = document.createElement('span');
  detail.textContent = presentation.text;
  copy.append(label, detail);
  row.appendChild(copy);

  const preferredTypes = key === 'exact_input'
    ? ['scoped_assertion']
    : (key === 'formalisation' ? ['ontology_mutation_receipt', 'scoped_assertion'] : []);
  const reference = referenceForReceipt(receipt, manifest, preferredTypes);
  if (reference) {
    const inspect = document.createElement('button');
    inspect.type = 'button';
    inspect.className = 'btn-mini concept-qa-receipt-inspect';
    inspect.textContent = 'Inspect';
    inspect.title = `Inspect ${presentation.label.toLowerCase()}`;
    inspect.addEventListener('click', () => {
      void onInspect(reference, { trigger: inspect });
    });
    row.appendChild(inspect);
  }
  const confirmation = receipt?.confirmation;
  const assertionId = cleanText(receipt?.assertion_id);
  if (
    key === 'formalisation'
    && confirmation?.available === true
    && assertionId
    && typeof onConfirm === 'function'
  ) {
    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'btn-mini concept-qa-receipt-confirm';
    confirm.dataset.assertionId = assertionId;
    confirm.dataset.receiptConfirmationAvailable = 'true';
    confirm.dataset.availableAfterFinish = confirmation.available_after_finish === true
      ? 'true'
      : 'false';
    confirm.dataset.availableAfterCancel = confirmation.available_after_cancel === true
      ? 'true'
      : 'false';
    applyConfirmationUiState(
      confirm,
      resolveConfirmationUiState(confirmationOptions, receipt),
    );
    confirm.addEventListener('click', async () => {
      if (confirm.disabled) return;
      confirm.disabled = true;
      confirm.textContent = 'Confirming…';
      try {
        await onConfirm(receipt, { button: confirm });
      } finally {
        if (confirm.isConnected) {
          applyConfirmationUiState(
            confirm,
            resolveConfirmationUiState(confirmationOptions, receipt),
          );
        }
      }
    });
    row.appendChild(confirm);
  }
  container.appendChild(row);
}

export function renderConceptQaReceipt(container, value, options = {}) {
  if (!(container instanceof Element)) return null;
  const receipts = normaliseConceptQaReceipts(value);
  if (!hasReceiptSignal(receipts)) return null;
  const manifest = normaliseReferenceManifest(
    value?.reference_manifest
      || value?.llm_debug_data?.reference_manifest
      || value?.llm_debug?.reference_manifest,
  );
  const onInspect = typeof options.onInspect === 'function'
    ? options.onInspect
    : openReferenceInspector;
  const onConfirm = typeof options.onConfirm === 'function' ? options.onConfirm : null;

  const panel = document.createElement('section');
  panel.className = 'concept-qa-receipt';
  panel.setAttribute('aria-label', 'Concept Q&A representation receipt');
  const heading = document.createElement('h4');
  heading.className = 'concept-qa-receipt-title';
  heading.textContent = 'What was recorded';
  panel.appendChild(heading);

  appendReceiptRow(
    panel,
    'exact_input',
    receipts.exact_input,
    exactInputPresentation(receipts.exact_input),
    manifest,
    onInspect,
    onConfirm,
    options,
  );
  appendReceiptRow(
    panel,
    'formalisation',
    receipts.formalisation,
    formalisationPresentation(receipts.formalisation),
    manifest,
    onInspect,
    onConfirm,
    options,
  );
  appendReceiptRow(
    panel,
    'notes',
    receipts.notes,
    notesPresentation(receipts.notes),
    manifest,
    onInspect,
    onConfirm,
    options,
  );
  container.appendChild(panel);
  return panel;
}

export const _test = {
  exactInputPresentation,
  formalisationPresentation,
  notesPresentation,
  isMembershipCandidate,
  normaliseConfirmationUiState,
};
