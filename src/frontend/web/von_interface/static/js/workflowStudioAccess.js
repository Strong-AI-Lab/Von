// Same responsive boundary as the mobile application shell (PR #631).
export const STUDIO_MOBILE_QUERY = '(max-width: 800px), (max-width: 1024px) and (pointer: coarse)';
let admin = false;

export function canUseWorkflowStudio() {
  return admin && !window.matchMedia?.(STUDIO_MOBILE_QUERY).matches;
}

export function setWorkflowStudioAccess(authStatus) {
  admin = authStatus?.authenticated === true && authStatus?.workflow_studio_access === true;
}

export function setupWorkflowStudioAccess(onUnavailable) {
  const sync = () => {
    const allowed = canUseWorkflowStudio();
    const button = document.querySelector('[data-tab="workflowStudioTab"]');
    if (button) button.hidden = !allowed;
    if (!allowed && document.getElementById('workflowStudioTab')?.classList.contains('active')) {
      onUnavailable();
    }
  };
  window.matchMedia?.(STUDIO_MOBILE_QUERY).addEventListener('change', sync);
  document.addEventListener('authStatusChanged', event => {
    setWorkflowStudioAccess(event.detail);
    sync();
  });
  sync();
}
