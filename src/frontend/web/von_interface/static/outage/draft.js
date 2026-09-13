// A tab-local unsent-text recovery copy, never an API/history cache or send queue.
// Read only after canonical authentication and organisation bootstrap have succeeded.
export function preserveOutageDraft(getNamespace) {
  const namespace = getNamespace();
  if (typeof namespace !== 'string' || !namespace) return;
  const input = document.getElementById('promptInput');
  if (!input) return;
  const key = `von:unsentReloadDraft:v1:${namespace}`;
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(key)); } catch { /* optional storage */ }
  if (typeof saved?.text === 'string' && saved.text) {
    const recovery = document.createElement('details');
    recovery.id = 'vonRecoveredDraft';
    const title = document.createElement('summary');
    title.textContent = 'Unsent draft recovered — review before sending';
    const message = document.createElement('p');
    message.textContent = 'This text was saved in this tab before the page reloaded. Check the conversation and any interrupted send before reusing it. Attachments are not stored here.';
    const text = document.createElement('textarea');
    text.readOnly = true;
    text.value = saved.text;
    text.setAttribute('aria-label', 'Recovered unsent draft');
    text.style.cssText = 'box-sizing:border-box;width:100%;min-height:6rem';
    const discard = document.createElement('button');
    discard.type = 'button';
    discard.textContent = 'Discard recovered draft';
    discard.addEventListener('click', () => {
      try { sessionStorage.removeItem(key); } catch { /* optional */ }
      recovery.remove();
    });
    recovery.append(title, message, text, discard);
    input.parentElement.before(recovery);
  }
  document.addEventListener('orgSwitchStarted', () => {
    document.getElementById('vonRecoveredDraft')?.remove();
  }, { once: true });
  const saveDraft = () => {
    // Programmatic clearing after send is observed here too. Never restore it as
    // an unsent prompt or automatically replay an uncertain effect.
    if (getNamespace() !== namespace) return;
    const text = input.value;
    try {
      if (text) sessionStorage.setItem(key, JSON.stringify({ text }));
      else if (!document.getElementById('vonRecoveredDraft')) sessionStorage.removeItem(key);
    } catch { /* The open page still keeps its draft if storage is unavailable. */ }
  };
  window.addEventListener('pagehide', saveDraft);
  return () => window.removeEventListener('pagehide', saveDraft);
}
