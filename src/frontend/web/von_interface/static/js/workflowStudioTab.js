// The Studio document and modules are requested only when its tab is activated.
export async function loadWorkflowStudioTab(container) {
  if (container.dataset.initialized === 'true' || container.dataset.loading === 'true') return;
  container.dataset.loading = 'true';
  container.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch(container.dataset.src);
    if (!response.ok) throw new Error(`Workflow Studio returned HTTP ${response.status}`);
    const html = await response.text();
    const [studio, monitor] = await Promise.all([
      import('./workflowStudioPage.js'),
      import('./chatTab.js')
    ]);
    container.innerHTML = html;
    monitor.initializeWorkflowStatusPanel();
    // Initialise synchronously before the catalogue request; returning to the
    // tab keeps the user's selection and any draft edits.
    void studio.initialiseWorkflowStudio();
    container.dataset.initialized = 'true';
  } catch (error) {
    console.error('Could not open Workflow Studio:', error);
    container.innerHTML = '<div class="error" role="alert">Could not load Workflow Studio. <button type="button" class="btn-mini">Retry</button></div>';
    container.querySelector('button').addEventListener('click', () => {
      void loadWorkflowStudioTab(container);
    });
  } finally {
    delete container.dataset.loading;
    container.setAttribute('aria-busy', 'false');
  }
}
