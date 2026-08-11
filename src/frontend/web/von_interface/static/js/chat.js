import { annotateTurn, postJson } from './apiService.js';
import {
  addMessageToChat,
  elements,
  getCurrentUser,
  renderSpanSuggestions,
} from './domUtils.js';

/**
 * Show/hide the thinking card wrapper (or legacy loadingIndicator).
 * JVNAUTOSCI-1080: Use aria-hidden on wrapper instead of inline display.
 */
function setPresenterThinkingState(isThinking) {
  const wrapper = document.getElementById('thinkingCardWrapper');
  if (wrapper) {
    wrapper.setAttribute('aria-hidden', isThinking ? 'false' : 'true');
  } else if (elements.loadingIndicator) {
    // Legacy fallback for pages without thinking card wrapper
    elements.loadingIndicator.style.display = isThinking ? 'block' : 'none';
  }
}

export async function sendMessageToServer() {
  const prompt = elements.promptInput.value.trim();
  if (!prompt) return;
  setPresenterThinkingState(true);
  if (elements.vonIntro) elements.vonIntro.style.display = 'none';
  const userTurnId = `u-${Date.now()}`;
  addMessageToChat('user', prompt, { turnId: userTurnId });
  elements.promptInput.value = '';
  try {
    // JVNAUTOSCI-2130: Forward thinking-card mode if the user has selected
    // an expert/debug variant via the main chat tab (stored in localStorage).
    let thinkingCardMode = 'default';
    try {
      const stored = window.localStorage.getItem('von:thinkingCardMode');
      if (stored === 'expert' || stored === 'debug' || stored === 'default') {
        thinkingCardMode = stored;
      }
    } catch (_err) {
      // Ignore storage access errors; fall back to default.
    }
    const data = await postJson('/von/generate', {
      prompt,
      presenter_mode: true,
      thinking_card_mode: thinkingCardMode,
    });
    const assistantText = data.response || data.message || 'No response';
    const assistantTurnId = `a-${Date.now()}`;
    addMessageToChat('assistant', assistantText, { turnId: assistantTurnId });

    // Send annotations for both turns (user and assistant). For now we send minimal payloads.
    try {
      // Annotate user turn
      annotateTurn({
        conversation_id: elements.conversationId || 'local',
        turn_id: userTurnId,
        speaker: 'user',
        text: prompt
      }).then(() => { }).catch((e) => console.debug('Annotation user failed', e));

      // Annotate assistant turn and render suggestions inline when available
      annotateTurn({
        conversation_id: elements.conversationId || 'local',
        turn_id: assistantTurnId,
        speaker: 'assistant',
        text: assistantText
      }).then((resp) => {
        console.info('[annotations] annotateTurn response', resp);
        if (resp && resp.suggestions) {
          renderSpanSuggestions(assistantTurnId, resp.suggestions);
        }
      }).catch((e) => console.info('Annotation assistant failed', e));
    } catch (e) {
      console.debug('Annotation pipeline error', e);
    }
  } catch (err) {
    console.error(err);
    addMessageToChat('system', `Error: ${err.message}`);
  } finally {
    setPresenterThinkingState(false);
  }
}

export async function resetChat() {
  try {
    await postJson('/von/reset', {});
    if (elements.scrollableField) elements.scrollableField.innerHTML = '';
    if (elements.vonIntro) elements.vonIntro.style.display = 'block';
  } catch (err) {
    console.error(err);
    addMessageToChat('system', `Error resetting chat: ${err.message}`);
  }
}

// Handle candidate selection events emitted by the DOM helper
document.addEventListener('annotation:candidateSelected', (ev) => {
  const { turnId, span, candidate } = ev.detail || {};
  console.debug('Candidate selected', turnId, span, candidate);
  // Show a small timing indicator while we persist the acceptance
  const start = Date.now();
  try {
    // optimistic UI update: show immediate timing placeholder
    import('./domUtils.js').then(mod => mod.setAnnotationTiming(turnId, '…'));
  } catch (_) { }

  // POST acceptance to backend
  (async () => {
    try {
      const resp = await fetch('/api/annotations/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_id: turnId, span: span, candidate: candidate, user_id: getCurrentUser().id })
      });
      const elapsed = Date.now() - start;
      try { import('./domUtils.js').then(mod => mod.setAnnotationTiming(turnId, elapsed)); } catch (_) { }
      if (!resp.ok) {
        console.warn('Annotation accept failed', resp.status);
      } else {
        console.debug('Annotation accepted persisted', await resp.json());
        // Optimistic UI: mark candidate as accepted and show undo
        try { import('./domUtils.js').then(mod => mod.markCandidateAccepted(turnId, candidate.id || candidate.concept_id || candidate.conceptId || candidate.name, candidate.name || candidate.id)); } catch (_) { }
      }
    } catch (e) {
      const elapsed = Date.now() - start;
      try { import('./domUtils.js').then(mod => mod.setAnnotationTiming(turnId, elapsed)); } catch (_) { }
      console.error('Error sending accept', e);
    }
  })();
});

// Undo handler: call revoke endpoint and revert UI
document.addEventListener('annotation:undoRequested', (ev) => {
  const { turnId, candidateId } = ev.detail || {};
  (async () => {
    try {
      const resp = await fetch('/api/annotations/revoke', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_id: turnId, candidate_id: candidateId })
      });
      if (resp.ok) {
        import('./domUtils.js').then(mod => mod.clearAcceptedMark(turnId));
      } else {
        console.warn('Annotation revoke failed', resp.status);
      }
    } catch (e) {
      console.error('Error revoking annotation', e);
    }
  })();
});

// Handle create requests (user wants to create a new concept for the span)
document.addEventListener('annotation:createRequested', (ev) => {
  const { turnId, span } = ev.detail || {};
  console.debug('Create requested for span', turnId, span);
  // TODO: show modal/form to capture new concept details and call backend create endpoint
});
