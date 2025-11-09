// Import/Export Tab functionality
let operationHistory = [];

// Helper: detect jsdom test environment
function isJSDOM() {
  try {
    return typeof navigator !== 'undefined' && /jsdom/i.test(navigator.userAgent || '');
  } catch {
    return false;
  }
}

// Helper: safely trigger a file download, avoiding jsdom navigation errors in tests
function safeDownloadBlob(blob, filename) {
  const url = window.URL.createObjectURL(blob);
  if (!isJSDOM()) {
    const a = document.createElement('a');
    a.href = url;
    if (filename) a.download = filename;
    // Append and click only in real browser
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
  window.URL.revokeObjectURL(url);
}

export function initializeImportExportTab() {
  console.log('Initializing Import/Export tab...');

  // Initialize all event listeners
  setupDirectOntologyManagement();
  setupVontologyImportExport();
  setupEntityImportExport();
  setupOperationHistory();

  // Conditional access: currently ALWAYS disabled (governance hardening).
  // Future re-enable path: backend settings may expose flags
  //  - direct_ontology_management_enable_for_privileged
  //  - direct_ontology_management_enable_globally
  // When absent or falsy, panel remains disabled for all users including prior privileged user.
  applyDirectOntologyAccessControl();

  console.log('Import/Export tab initialization complete.');
}

function setupDirectOntologyManagement() {
  // Direct Ontology Export
  const exportButton = document.getElementById('exportOntologyDirectButton');
  const repairButton = document.getElementById('repairOntologyButton');
  const importFileInput = document.getElementById('ontologyDirectImportFile');
  const importButton = document.getElementById('importOntologyDirectButton');
  const statusElement = document.getElementById('ontologyDirectStatusMessage');
  const progressContainer = document.getElementById('ontologyDirectProgress');
  const progressBar = document.getElementById('ontologyDirectProgressBar');
  const progressText = document.getElementById('ontologyDirectProgressText');

  if (exportButton) {
    exportButton.addEventListener('click', async () => {
      await exportDirectOntology(exportButton, statusElement);
    });
  }

  if (repairButton) {
    repairButton.addEventListener('click', async () => {
      await repairOntologyDatabase(repairButton, statusElement);
    });
  }

  if (importFileInput) {
    importFileInput.addEventListener('change', () => {
      handleDirectImportFileSelection(importFileInput, importButton, statusElement);
    });
    // Handle race: if a file was already chosen before listeners attached, enable immediately.
    const preSelected = (importFileInput.files && importFileInput.files.length > 0) || importFileInput.value;
    if (preSelected) {
      handleDirectImportFileSelection(importFileInput, importButton, statusElement);
    }
  }

  if (importButton) {
    importButton.addEventListener('click', async () => {
      await importDirectOntology(importFileInput, importButton, statusElement, progressContainer, progressBar, progressText);
    });
  }
}

// Disable the Direct Ontology Management panel unless explicit enable flags are present in settings.
async function applyDirectOntologyAccessControl() {
  try {
    const panel = document.querySelector('.import-export-direct-management');
    if (!panel) return;
    // Fetch current settings (may contain future enable flags)
    const resp = await fetch('/api/settings/');
    let settings = {};
    if (resp.ok) {
      try { settings = await resp.json(); } catch { /* ignore */ }
    }

    const enablePrivileged = !!settings.direct_ontology_management_enable_for_privileged;
    const enableGlobal = !!settings.direct_ontology_management_enable_globally;
    // Even if user was previously privileged we now require explicit flags.
    const shouldEnable = enableGlobal || enablePrivileged; // Privileged scoping (by concept id) intentionally deferred until flag introduced.

    if (!shouldEnable) {
      if (!panel.classList.contains('disabled-panel')) panel.classList.add('disabled-panel');
      let badge = panel.querySelector('.disabled-badge');
      if (!badge) {
        badge = document.createElement('div');
        badge.className = 'disabled-badge';
        badge.textContent = 'DISABLED';
        panel.prepend(badge);
      }
      panel.querySelectorAll('button, input[type="file"]').forEach(el => { el.disabled = true; });
      const noteId = 'directOntologyDisabledNote';
      if (!document.getElementById(noteId)) {
        const p = document.createElement('p');
        p.id = noteId;
        p.className = 'import-export-small-text text-danger';
        p.textContent = 'Direct Ontology Management is currently disabled for all users (governance hardening in effect).';
        panel.appendChild(p);
      }
    }
  } catch (e) {
    console.warn('Failed applying direct ontology access control:', e);
  }
}

async function exportDirectOntology(button, statusElement) {
  try {
    // Show loading state
    button.disabled = true;
    button.textContent = '📥 Exporting...';
    showDirectOntologyStatus(statusElement, 'Preparing complete ontology export...', false);
    addToHistory('Direct Ontology Export', 'Started complete ontology export', 'info');

    // Make request to export endpoint
    const response = await fetch('/api/settings/export-ontology', {
      method: 'GET',
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.error || `Export failed: ${response.statusText}`);
    }

    // Get the filename from the response headers
    const contentDisposition = response.headers.get('Content-Disposition');
    let filename = 'von_ontology_export.json';
    if (contentDisposition) {
      const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/);
      if (filenameMatch) {
        filename = filenameMatch[1];
      }
    }

    // Create download
    const blob = await response.blob();
    safeDownloadBlob(blob, filename);

    showDirectOntologyStatus(statusElement, `✅ Successfully exported complete ontology as ${filename}`, false);
    addToHistory('Direct Ontology Export', `Successfully exported as ${filename}`, 'success');
  } catch (error) {
    console.error('Direct export error:', error);
    showDirectOntologyStatus(statusElement, `❌ Export failed: ${error.message}`, true);
    addToHistory('Direct Ontology Export', `Failed: ${error.message}`, 'error');
  } finally {
    // Reset button state
    button.disabled = false;
    button.textContent = '📥 Export Complete Ontology';
  }
}

function handleDirectImportFileSelection(fileInput, importButton, statusElement) {
  // Support race condition where user selects a file before listeners attach.
  // Treat a non-empty value attribute (jsdom limitation) as indicative of selection.
  const hasFile = (fileInput.files && fileInput.files.length > 0) || !!fileInput.value;
  if (hasFile) {
    const file = (fileInput.files && fileInput.files[0]) || { name: fileInput.value.split(/[\\/]/).pop(), size: 0 };

    // Validate file type
    if (!file.name.toLowerCase().endsWith('.json')) {
      showDirectOntologyStatus(statusElement, '❌ Please select a JSON file', true);
      importButton.disabled = true;
      return;
    }

    // Show file info and enable import button
    showDirectOntologyStatus(statusElement, `📁 Selected: ${file.name} (${(file.size / 1024 / 1024).toFixed(2)} MB)`, false);
    importButton.disabled = false;
  } else {
    importButton.disabled = true;
    showDirectOntologyStatus(statusElement, '', false);
  }
}

async function importDirectOntology(fileInput, importButton, statusElement, progressContainer, progressBar, progressText) {
  if (fileInput.files.length === 0) {
    showDirectOntologyStatus(statusElement, '❌ Please select a file first', true);
    return;
  }

  const file = fileInput.files[0];

  // Confirm the destructive operation
  const confirmed = confirm(
    `⚠️ WARNING: This will replace ALL existing concepts in the database.\n\n` +
    `File: ${file.name}\n` +
    `Size: ${(file.size / 1024 / 1024).toFixed(2)} MB\n\n` +
    `A backup will be created automatically before import.\n\n` +
    `Are you sure you want to continue?`
  );

  if (!confirmed) {
    return;
  }

  try {
    // Show loading state
    importButton.disabled = true;
    importButton.textContent = '📤 Importing...';
    progressContainer.style.display = 'block';
    progressBar.style.width = '10%';
    progressText.textContent = 'Validating file...';
    showDirectOntologyStatus(statusElement, '🔄 Starting complete ontology import...', false);
    addToHistory('Direct Ontology Import', `Started import of ${file.name}`, 'info');

    // Create FormData
    const formData = new FormData();
    formData.append('file', file);

    // Update progress
    progressBar.style.width = '30%';
    progressText.textContent = 'Uploading file...';

    // Make request to import endpoint
    const response = await fetch('/api/settings/import-ontology', {
      method: 'POST',
      body: formData
    });

    progressBar.style.width = '80%';
    progressText.textContent = 'Processing data...';

    const result = await response.json();

    if (!response.ok) {
      let errorMessage = result.error || `Import failed: ${response.statusText}`;

      // Add validation details if available
      if (result.details && result.details.length > 0) {
        errorMessage += '\n\nValidation errors:';
        result.details.slice(0, 5).forEach(detail => {
          errorMessage += '\n• ' + detail;
        });

        if (result.total_errors > 5) {
          errorMessage += `\n... and ${result.total_errors - 5} more errors`;
        }
      }

      throw new Error(errorMessage);
    }

    // Complete progress
    progressBar.style.width = '100%';
    progressText.textContent = 'Import complete!';

    // Show success message
    showDirectOntologyStatus(
      statusElement,
      `✅ ${result.message}\n📊 Imported: ${result.imported_count} concepts\n💾 Backup: ${result.backup_collection}`,
      false
    );
    addToHistory('Direct Ontology Import', `Successfully imported ${result.imported_count} concepts`, 'success');

    // Clear file input
    fileInput.value = '';
    importButton.disabled = true;

    // Auto-hide progress after delay
    setTimeout(() => {
      progressContainer.style.display = 'none';
    }, 3000);

  } catch (error) {
    console.error('Direct import error:', error);
    showDirectOntologyStatus(statusElement, `❌ Import failed: ${error.message}`, true);
    addToHistory('Direct Ontology Import', `Failed: ${error.message}`, 'error');
    progressContainer.style.display = 'none';
  } finally {
    // Reset button state
    importButton.disabled = true;
    importButton.textContent = '📤 Replace All Concepts';
  }
}

async function repairOntologyDatabase(button, statusElement) {
  try {
    // Update button state
    button.disabled = true;
    button.textContent = '🔧 Repairing...';

    showDirectOntologyStatus(statusElement, '🔄 Repairing database - finding concepts with missing concept_ids...', false);
    addToHistory('Ontology Repair', 'Started database repair process', 'info');

    const response = await fetch('/api/settings/repair-ontology', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      }
    });

    const result = await response.json();

    if (!response.ok) {
      throw new Error(result.error || `Repair failed: ${response.statusText}`);
    }

    // Show success message
    let successMessage = `✅ ${result.message}`;
    if (result.repaired_count > 0) {
      successMessage += `\n📊 Repaired: ${result.repaired_count} concepts`;
      successMessage += `\n💾 Backup: ${result.backup_collection}`;

      if (result.repair_details && result.repair_details.length > 0) {
        successMessage += '\n\nRepaired concepts:';
        result.repair_details.slice(0, 3).forEach(detail => {
          successMessage += `\n• ${detail.generated_concept_id} (based on ${detail.based_on})`;
        });

        if (result.repair_details.length > 3) {
          successMessage += `\n... and ${result.repair_details.length - 3} more`;
        }
      }
    }

    showDirectOntologyStatus(statusElement, successMessage, false);
    addToHistory('Ontology Repair', result.message, 'success');

  } catch (error) {
    console.error('Ontology repair error:', error);
    showDirectOntologyStatus(statusElement, `❌ Repair failed: ${error.message}`, true);
    addToHistory('Ontology Repair', `Failed: ${error.message}`, 'error');
  } finally {
    // Reset button state
    button.disabled = false;
    button.textContent = '🔧 Repair Database';
  }
}

function showDirectOntologyStatus(statusElement, message, isError) {
  if (statusElement) {
    statusElement.textContent = message;
    statusElement.style.display = message ? 'block' : 'none';
    statusElement.style.backgroundColor = isError ? '#f8d7da' : '#d4edda';
    statusElement.style.color = isError ? '#721c24' : '#155724';
    statusElement.style.border = isError ? '1px solid #f5c6cb' : '1px solid #c3e6cb';
    statusElement.style.whiteSpace = 'pre-line'; // Allow line breaks in error messages

    // Auto-hide success messages after 5 seconds
    if (!isError && message) {
      setTimeout(() => {
        statusElement.style.display = 'none';
      }, 5000);
    }
  }
}

function setupVontologyImportExport() {
  // Vontology Import
  const vontologyFileInput = document.getElementById('vontologyFileInput');
  const importVontologyButton = document.getElementById('importVontologyButton');
  const previewVontologyImportButton = document.getElementById('previewVontologyImportButton');
  const vontologyImportStatus = document.getElementById('vontologyImportStatus');

  // Vontology Export
  const exportVontologyButton = document.getElementById('exportVontologyButton');
  const previewVontologyExportButton = document.getElementById('previewVontologyExportButton');
  const vontologyExportStatus = document.getElementById('vontologyExportStatus');

  if (vontologyFileInput && importVontologyButton) {
    const updateVontologyButtons = () => {
      const hasFile = (vontologyFileInput.files && vontologyFileInput.files.length > 0) || !!vontologyFileInput.value;
      importVontologyButton.disabled = !hasFile;
      if (previewVontologyImportButton) {
        previewVontologyImportButton.disabled = !hasFile;
      }
    };
    vontologyFileInput.addEventListener('change', updateVontologyButtons);
    // Handle race: if file selected before init, enable immediately.
    updateVontologyButtons();

    importVontologyButton.addEventListener('click', () => {
      importVontology(vontologyFileInput, vontologyImportStatus, false);
    });

    if (previewVontologyImportButton) {
      previewVontologyImportButton.addEventListener('click', () => {
        importVontology(vontologyFileInput, vontologyImportStatus, true);
      });
    }
  }

  if (exportVontologyButton) {
    exportVontologyButton.addEventListener('click', () => {
      exportVontology(vontologyExportStatus, false);
    });
  }

  if (previewVontologyExportButton) {
    previewVontologyExportButton.addEventListener('click', () => {
      exportVontology(vontologyExportStatus, true);
    });
  }
}

function setupEntityImportExport() {
  // Entity Import
  const entityFileInput = document.getElementById('entityFileInput');
  const importEntityButton = document.getElementById('importEntityButton');
  const previewEntityButton = document.getElementById('previewEntityButton');
  const entityImportStatus = document.getElementById('entityImportStatus');

  // Entity Export
  const exportEntityButton = document.getElementById('exportEntityButton');
  const previewEntityExportButton = document.getElementById('previewEntityExportButton');
  const entityExportStatus = document.getElementById('entityExportStatus');

  if (entityFileInput && importEntityButton && previewEntityButton) {
    const updateEntityButtons = () => {
      const hasFile = (entityFileInput.files && entityFileInput.files.length > 0) || !!entityFileInput.value;
      importEntityButton.disabled = !hasFile;
      previewEntityButton.disabled = !hasFile;
    };
    entityFileInput.addEventListener('change', updateEntityButtons);
    updateEntityButtons();

    importEntityButton.addEventListener('click', () => {
      importEntities(entityFileInput, entityImportStatus, false);
    });

    previewEntityButton.addEventListener('click', () => {
      importEntities(entityFileInput, entityImportStatus, true);
    });
  }

  if (exportEntityButton && previewEntityExportButton) {
    exportEntityButton.addEventListener('click', () => {
      exportEntities(entityExportStatus, false);
    });

    previewEntityExportButton.addEventListener('click', () => {
      exportEntities(entityExportStatus, true);
    });
  }
}

function setupOperationHistory() {
  const clearHistoryButton = document.getElementById('clearHistoryButton');

  if (clearHistoryButton) {
    clearHistoryButton.addEventListener('click', () => {
      clearOperationHistory();
    });
  }

  // Initialize history display
  updateHistoryDisplay();
}

// Vontology Import Function
async function importVontology(fileInput, statusElement, isPreview = false) {
  const file = fileInput.files[0];
  if (!file) {
    updateStatus(statusElement, 'No file selected.', 'error');
    return;
  }

  const operationType = isPreview ? 'Vontology Import Preview' : 'Vontology Import';
  updateStatus(statusElement, `${operationType}...`, 'info');

  try {
    if (isPreview) {
      // BACKEND-DRIVEN PREVIEW
      const formData = new FormData();
      formData.append('file', file);
      let response;
      let backendFailed = false;
      try {
        response = await fetch('/vontology/api/vontology/import_preview', {
          method: 'POST',
          body: formData
        });
      } catch (e) {
        backendFailed = true;
      }

      if (response && response.ok) {
        const result = await response.json();
        if (result.success && result.analysis) {
          const analysis = result.analysis;
          renderVontologyImportAnalysis(analysis);
          const total = analysis.total_nodes || 0;
          const fmt = analysis.format_detected || 'unknown';
          const message = `Preview: ${total} nodes (${fmt} format)`;
          updateStatus(statusElement, message, 'success');
          addToHistory(operationType, message, 'success');
          return; // Done
        } else {
          backendFailed = true; // treat as failure and fall through to legacy preview
        }
      } else if (response && !response.ok) {
        backendFailed = true;
      }

      // FALLBACK: legacy client-side preview if backend unavailable or failed
      if (backendFailed) {
        try {
          const fileContent = await file.text();
          const data = JSON.parse(fileContent);
          let nodes = [];
          if (data && data.nodes && Array.isArray(data.nodes)) {
            nodes = data.nodes;
          } else if (Array.isArray(data)) {
            nodes = data;
          } else if (data && typeof data === 'object') {
            nodes = Object.values(data);
          }
          const message = `Preview (fallback): Would import ${nodes.length} vontology nodes`;
          updateStatus(statusElement, message, 'warning');
          addToHistory(operationType, message, 'warning');
          displayVontologyImportPreview(data);
        } catch (err) {
          updateStatus(statusElement, `Preview failed: ${err.message}`, 'error');
          addToHistory(operationType, `Failed: ${err.message}`, 'error');
        }
      }
    } else {
      // For actual import, use async mode to enable progress polling
      const formData = new FormData();
      formData.append('file', file);

      const importBtn = document.getElementById('importVontologyButton');
      const previewBtn = document.getElementById('previewVontologyImportButton');
      if (importBtn) importBtn.disabled = true;
      if (previewBtn) previewBtn.disabled = true;

      updateStatus(statusElement, 'Starting import (uploading file)...', 'info');

      const startResp = await fetch('/vontology/api/vontology/import_nodes?async=1', {
        method: 'POST',
        body: formData
      });
      if (!startResp.ok) {
        throw new Error(`Failed to start import (status ${startResp.status})`);
      }
      const startData = await startResp.json();
      if (!startData.success || !startData.job_id) {
        throw new Error('Import start did not return job id');
      }
      const jobId = startData.job_id;
      let lastProcessed = 0;
      let pollAttempts = 0;

      const poll = async () => {
        try {
          const r = await fetch(`/vontology/api/vontology/import_progress/${jobId}`);
          if (!r.ok) throw new Error(`Progress HTTP ${r.status}`);
          const pdata = await r.json();
          if (!pdata.success) throw new Error('Progress response unsuccessful');
          const p = pdata.progress || {};
          const { processed = 0, total = 0, imported = 0, updated = 0, skipped = 0, last_concept_id, last_concept_name, status } = p;
          const action = p.action;
          // Build dynamic line
          const line = `Processing ${processed}/${total} | Added ${imported} • Updated ${updated} • Skipped ${skipped}`;
          const lastLine = last_concept_id ? ` | Last: ${last_concept_name || ''} (${last_concept_id})${action ? ' [' + action + ']' : ''}` : '';
          updateStatus(statusElement, line + lastLine, 'info');
          if (processed !== lastProcessed) {
            lastProcessed = processed;
          }
          if (status === 'completed') {
            const result = p.result || {};
            const importedF = result.imported_count || imported;
            const updatedF = result.updated_count || updated;
            const skippedF = result.skipped_count || skipped;
            const totalF = importedF + updatedF + skippedF;
            const convertedFromOpencyc = result.converted_from_opencyc || false;
            let message;
            if (convertedFromOpencyc) {
              message = `OpenCyc import successful! Processed ${totalF} concepts: Added ${importedF}, Updated ${updatedF}, Skipped ${skippedF}`;
            } else {
              // Align wording with expected user message pattern
              message = `Import successful! Processed ${totalF} nodes: Added ${importedF}, Updated ${updatedF}, Skipped ${skippedF}`;
            }
            updateStatus(statusElement, message, 'success');
            addToHistory('Vontology Import', message, 'success');
            // Re-enable buttons so user can run another preview/import without reselecting file
            if (importBtn) importBtn.disabled = false;
            if (previewBtn) previewBtn.disabled = false;
            return true; // done
          }
          if (status === 'error') {
            const errMsg = p.error || 'Import failed (async)';
            updateStatus(statusElement, `❌ ${errMsg}`, 'error');
            addToHistory('Vontology Import', errMsg, 'error');
            // Allow retry after error
            if (importBtn) importBtn.disabled = false;
            if (previewBtn) previewBtn.disabled = false;
            return true; // stop polling
          }
        } catch (e) {
          pollAttempts += 1;
          if (pollAttempts > 10) {
            updateStatus(statusElement, `Progress polling failed: ${e.message}`, 'error');
            addToHistory('Vontology Import', `Progress error: ${e.message}`, 'error');
            if (importBtn) importBtn.disabled = false;
            if (previewBtn) previewBtn.disabled = false;
            return true;
          }
        }
        return false;
      };

      // Poll loop
      let done = false;
      while (!done) {
        // eslint-disable-next-line no-await-in-loop
        done = await poll();
        if (!done) {
          // eslint-disable-next-line no-await-in-loop
          await new Promise(res => setTimeout(res, 800));
        }
      }
      // No extra button state handling here; handled inside poll completion/error branches
    }
  } catch (error) {
    console.error(`Error in ${operationType}:`, error);
    const errorMsg = `Error in ${operationType}: ${error.message}`;
    updateStatus(statusElement, errorMsg, 'error');
    addToHistory(operationType, errorMsg, 'error');
    // Ensure buttons can be retried if outer try/catch triggers
    const importBtn = document.getElementById('importVontologyButton');
    const previewBtn = document.getElementById('previewVontologyImportButton');
    if (importBtn) importBtn.disabled = false;
    if (previewBtn) previewBtn.disabled = false;
  }
}

// Vontology Export Function
async function exportVontology(statusElement, isPreview = false) {
  const operationType = isPreview ? 'Vontology Export Preview' : 'Vontology Export';
  updateStatus(statusElement, `${operationType}...`, 'info');

  try {
    const response = await fetch('/vontology/api/vontology/export_nodes');

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    if (isPreview) {
      // For preview mode, parse the JSON to analyze the data
      const result = await response.json();
      const message = `Preview: Would export ${Object.keys(result).length || 0} vontology nodes`;
      updateStatus(statusElement, message, 'success');
      addToHistory(operationType, message, 'success');
      displayVontologyPreview(result);
    } else {
      // For actual export, handle as blob for download
      const blob = await response.blob();
      safeDownloadBlob(blob, `vontology_export_${new Date().toISOString().split('T')[0]}.json`);

      const message = 'Vontology exported successfully!';
      updateStatus(statusElement, message, 'success');
      addToHistory('Vontology Export', message, 'success');
    }
  } catch (error) {
    console.error(`Error in ${operationType}:`, error);
    const errorMsg = `Error in ${operationType}: ${error.message}`;
    updateStatus(statusElement, errorMsg, 'error');
    addToHistory(operationType, errorMsg, 'error');
  }
}

// Concepts Import Function (with legacy fallback)
async function importEntities(fileInput, statusElement, isPreview = false) {
  const file = fileInput.files[0];
  if (!file) {
    updateStatus(statusElement, 'No file selected.', 'error');
    return;
  }

  const conflictResolution = document.querySelector('input[name="conflictResolution"]:checked')?.value || 'update';
  // default true only if checkbox missing; honor unchecked state
  const validateConcepts = document.getElementById('validateConceptsCheckbox')?.checked ?? true;
  const dryRun = isPreview || document.getElementById('dryRunCheckbox')?.checked || false;

  const operationType = isPreview ? 'Concepts Import Preview' : 'Concepts Import';
  updateStatus(statusElement, `${operationType}...`, 'info');

  try {
    if (isPreview) {
      // For preview mode, read and analyze the file client-side
      const fileContent = await file.text();
      const data = JSON.parse(fileContent);

      // Analyze the concepts data (support legacy shape)
      const concepts = Array.isArray(data) ? data : (data.concepts || data.entities || []);
      const message = `Preview: Would import ${concepts.length} concepts with ${conflictResolution} conflict resolution`;
      updateStatus(statusElement, message, 'success');
      addToHistory(operationType, message, 'success');
      displayEntityImportPreview(data, conflictResolution);
      return;
    }

    // For actual import, try the server endpoint
    const formData = new FormData();
    formData.append('file', file);
    formData.append('conflict_resolution', conflictResolution);
    formData.append('validate_concepts', validateConcepts);
    formData.append('dry_run', dryRun);

    // Try concepts-first endpoint with legacy fallback
    let endpointTried = '/api/concepts/import';
    let response = await fetch(endpointTried, {
      method: 'POST',
      body: formData
    });

    if (!response.ok && (response.status === 404 || response.status === 405)) {
      endpointTried = '/api/entities/import';
      response = await fetch(endpointTried, {
        method: 'POST',
        body: formData
      });
    }

    if (!response.ok) {
      // Handle the case where endpoint doesn't exist yet
      if (response.status === 404 || response.status === 405) {
        const message = `${operationType} endpoint not available (${endpointTried}).`;
        updateStatus(statusElement, message, 'warning');
        addToHistory(operationType, message, 'warning');
        return;
      }
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const result = await response.json();

    if (result.success) {
      const message = isPreview
        ? `Preview: Would import ${result.imported || 0} concepts (${result.created || 0} new, ${result.updated || 0} updated, ${result.skipped || 0} skipped)`
        : `Import successful! ${result.imported || 0} concepts processed (${result.created || 0} created, ${result.updated || 0} updated, ${result.skipped || 0} skipped)`;

      updateStatus(statusElement, message, 'success');
      addToHistory(operationType, message, 'success');

      // Show detailed results
      displayImportResults(result, isPreview);

      if (!isPreview) {
        // Clear the file input after successful import
        fileInput.value = '';
        document.getElementById('importEntityButton').disabled = true;
        document.getElementById('previewEntityButton').disabled = true;
      }
    } else {
      const errorMsg = result.error || `${operationType} failed.`;
      updateStatus(statusElement, errorMsg, 'error');
      addToHistory(operationType, errorMsg, 'error');
    }
  } catch (error) {
    console.error(`Error in ${operationType}:`, error);
    let errorMsg;
    if (error.message.includes('404') || error.message.includes('405') || error.message.includes('Failed to fetch')) {
      errorMsg = `${operationType} endpoint not available. Please ensure the backend is running and supports concepts import.`;
      updateStatus(statusElement, errorMsg, 'warning');
      addToHistory(operationType, errorMsg, 'warning');
    } else {
      errorMsg = `Error in ${operationType}: ${error.message}`;
      updateStatus(statusElement, errorMsg, 'error');
      addToHistory(operationType, errorMsg, 'error');
    }
  }
}

// Entity Export Function
async function exportEntities(statusElement, isPreview = false) {
  const conceptFilter = document.getElementById('conceptFilter')?.value.trim() || '';
  const includeDescendants = document.getElementById('includeDescendantsCheckbox')?.checked !== false; // Default to true
  const dateAfter = document.getElementById('dateAfterFilter')?.value || '';
  const updatedAfter = document.getElementById('updatedAfterFilter')?.value || '';

  const operationType = isPreview ? 'Concepts Export Preview' : 'Concepts Export';
  updateStatus(statusElement, `${operationType}...`, 'info');

  try {
    // Build query parameters compatible with backend API
    const params = new URLSearchParams();
    if (conceptFilter) params.append('concept_id', conceptFilter);
    params.append('include_descendants', includeDescendants ? 'true' : 'false');
    params.append('format', 'json');

    // Note: Backend API doesn't support date filters yet, we'll handle these client-side if needed
    // if (dateAfter) params.append('created_after', dateAfter);
    // if (updatedAfter) params.append('updated_after', updatedAfter);

    // Call concepts endpoint first; if a legacy entities endpoint exists, accept it as fallback
    let response = await fetch(`/api/concepts/export?${params}`);
    if (!response.ok && (response.status === 404 || response.status === 405)) {
      response = await fetch(`/api/entities/export?${params}`);
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP error! status: ${response.status}`);
    }

    const result = await response.json();

    if (isPreview) {
      const message = `Preview: Would export ${result.total_count || 0} concepts`;
      updateStatus(statusElement, message, 'success');
      addToHistory(operationType, message, 'success');
      displayExportPreview(result);
    } else {
      // Convert the response to a downloadable JSON file
      const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' });
      // Enhanced filename with concept filter info
      let filename = `concepts_export_${new Date().toISOString().split('T')[0]}.json`;
      if (conceptFilter) {
        const conceptName = conceptFilter.replace('#V#', '').replace(/[^a-zA-Z0-9]/g, '_');
        filename = `concepts_export_${conceptName}_${new Date().toISOString().split('T')[0]}.json`;
      }
      safeDownloadBlob(blob, filename);

      const message = `Concepts exported successfully! ${result.total_count || 0} concepts downloaded.`;
      updateStatus(statusElement, message, 'success');
      addToHistory(operationType, message, 'success');
    }
  } catch (error) {
    console.error(`Error in ${operationType}:`, error);
    const errorMsg = `Error in ${operationType}: ${error.message}`;
    updateStatus(statusElement, errorMsg, 'error');
    addToHistory(operationType, errorMsg, 'error');
  }
}

// Helper Functions
function updateStatus(element, message, type = 'info') {
  if (!element) return;

  element.textContent = message;
  element.className = `status-${type}`;

  // Add CSS classes for different status types
  element.style.color = type === 'error' ? '#d32f2f' :
    type === 'success' ? '#2e7d32' :
      type === 'warning' ? '#f57c00' : '#666';
  element.style.fontWeight = type === 'error' || type === 'success' ? 'bold' : 'normal';
}

function addToHistory(operation, message, status) {
  const timestamp = new Date().toLocaleString();
  operationHistory.unshift({
    timestamp,
    operation,
    message,
    status
  });

  // Keep only last 20 operations
  operationHistory = operationHistory.slice(0, 20);
  updateHistoryDisplay();
}

function updateHistoryDisplay() {
  const historyElement = document.getElementById('operationHistory');
  if (!historyElement) return;

  if (operationHistory.length === 0) {
    historyElement.innerHTML = '<p style="color: #999; font-style: italic;">No operations performed yet.</p>';
    return;
  }

  const historyHtml = operationHistory.map(entry => {
    const statusColor = entry.status === 'error' ? '#d32f2f' :
      entry.status === 'success' ? '#2e7d32' :
        entry.status === 'warning' ? '#f57c00' : '#666';
    return `
      <div style="margin-bottom: 8px; padding: 5px; border-left: 3px solid ${statusColor}; background-color: #f9f9f9;">
        <div style="font-weight: bold; color: ${statusColor};">[${entry.timestamp}] ${entry.operation}</div>
        <div style="margin-top: 2px;">${entry.message}</div>
      </div>
    `;
  }).join('');

  historyElement.innerHTML = historyHtml;
}

function clearOperationHistory() {
  operationHistory = [];
  updateHistoryDisplay();
}

function displayImportResults(result, isPreview) {
  const resultsElement = document.getElementById('entityImportResults');
  const contentElement = document.getElementById('importResultsContent');

  if (!resultsElement || !contentElement) return;

  const title = isPreview ? 'Import Preview Results' : 'Import Results';
  const details = `
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 10px;">
      <div><strong>Total:</strong> ${result.imported || 0}</div>
      <div style="color: #2e7d32;"><strong>Created:</strong> ${result.created || 0}</div>
      <div style="color: #1976d2;"><strong>Updated:</strong> ${result.updated || 0}</div>
      <div style="color: #f57c00;"><strong>Skipped:</strong> ${result.skipped || 0}</div>
    </div>
    ${result.errors && result.errors.length > 0 ?
      `<div style="margin-top: 10px;">
        <strong style="color: #d32f2f;">Errors:</strong>
        <ul style="margin: 5px 0; padding-left: 20px;">
          ${result.errors.map(error => `<li style="color: #d32f2f;">${error}</li>`).join('')}
        </ul>
      </div>` : ''}
    ${result.warnings && result.warnings.length > 0 ?
      `<div style="margin-top: 10px;">
        <strong style="color: #f57c00;">Warnings:</strong>
        <ul style="margin: 5px 0; padding-left: 20px;">
          ${result.warnings.map(warning => `<li style="color: #f57c00;">${warning}</li>`).join('')}
        </ul>
      </div>` : ''}
  `;

  contentElement.innerHTML = details;
  resultsElement.style.display = 'block';
}

function displayEntityImportPreview(data, conflictResolution) {
  const resultsElement = document.getElementById('entityImportResults');
  const contentElement = document.getElementById('importResultsContent');

  if (!resultsElement || !contentElement) return;

  // Analyze the concepts data (support legacy shape)
  const concepts = Array.isArray(data) ? data : (data.concepts || data.entities || []);

  // Count concepts by concept_id
  const conceptCounts = {};
  concepts.forEach(item => {
    const concept = item.concept_id || 'No concept';
    conceptCounts[concept] = (conceptCounts[concept] || 0) + 1;
  });

  // Get sample concepts (first 10)
  const sampleConcepts = concepts.slice(0, 10);

  const details = `
    <div style="margin-bottom: 10px;">
      <strong>Concepts to import:</strong> ${concepts.length}
    </div>
    <div style="margin-bottom: 10px;">
      <strong>Conflict resolution:</strong> ${conflictResolution}
    </div>
    ${Object.keys(conceptCounts).length > 0 ?
      `<div style="margin-bottom: 10px;">
        <strong>Concepts by concept:</strong>
        <ul style="margin: 5px 0; padding-left: 20px;">
          ${Object.entries(conceptCounts).map(([concept, count]) =>
        `<li>${concept}: ${count} concepts</li>`
      ).join('')}
        </ul>
      </div>` : ''}
    ${sampleConcepts.length > 0 ?
      `<div>
        <strong>Sample concepts:</strong>
        <ul style="margin: 5px 0; padding-left: 20px; max-height: 150px; overflow-y: auto;">
          ${sampleConcepts.map(item =>
        `<li>${item.name || 'Unnamed'} (${item.concept_id || 'No concept'})</li>`
      ).join('')}
          ${concepts.length > 10 ? `<li><em>... and ${concepts.length - 10} more</em></li>` : ''}
        </ul>
      </div>` : ''}
    <div style="margin-top: 10px; font-size: 12px; color: #666;">
      <strong>Note:</strong> This is a client-side preview. Actual import will validate concepts and handle conflicts.
    </div>
  `;

  contentElement.innerHTML = details;
  resultsElement.style.display = 'block';
}

function displayExportPreview(result) {
  const previewElement = document.getElementById('entityExportPreview');
  const contentElement = document.getElementById('exportPreviewContent');

  if (!previewElement || !contentElement) return;

  const concepts = result.concepts || result.entities || [];
  const totalCount = result.total_count || concepts.length || 0;
  const metadata = result.export_metadata || {};

  const details = `
    <div style="margin-bottom: 10px;">
      <strong>Concepts to export:</strong> ${totalCount}
    </div>
    ${metadata.concept_id ?
      `<div style="margin-bottom: 10px;">
        <strong>Filtered by concept:</strong> ${metadata.concept_id}
        ${metadata.include_descendants ? ' (including descendants)' : ' (direct only)'}
      </div>` : ''}
    ${concepts && concepts.length > 0 ?
      `<div>
        <strong>Sample concepts:</strong>
        <ul style="margin: 5px 0; padding-left: 20px; max-height: 150px; overflow-y: auto;">
          ${concepts.slice(0, 10).map(item =>
        `<li>${item.name || 'Unnamed'} (${item.concept_id || 'No concept'})</li>`
      ).join('')}
          ${concepts.length > 10 ? `<li><em>... and ${concepts.length - 10} more</em></li>` : ''}
        </ul>
      </div>` : ''}
  `;

  contentElement.innerHTML = details;
  previewElement.style.display = 'block';
}

// Vontology Import/Export Preview Renderers (for jsdom tests and UI preview)
function displayVontologyImportPreview(data) {
  renderVontologyPreview('import', data);
}

function displayVontologyPreview(data) {
  renderVontologyPreview('export', data);
}

function renderVontologyPreview(mode, data) {
  const isImport = mode === 'import';
  const previewElement = document.getElementById(isImport ? 'vontologyImportPreview' : 'vontologyExportPreview');
  const contentElement = document.getElementById(isImport ? 'vontologyImportPreviewContent' : 'vontologyPreviewContent');
  if (!previewElement || !contentElement) return;

  // Normalize nodes array from various supported shapes
  let nodes = [];
  if (data && Array.isArray(data.nodes)) {
    nodes = data.nodes;
  } else if (Array.isArray(data)) {
    nodes = data;
  } else if (data && typeof data === 'object') {
    nodes = Object.values(data);
  }

  // Count by type
  const typeCounts = {};
  nodes.forEach(n => {
    const t = (n && n.type) ? String(n.type) : 'unknown';
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  });

  const total = nodes.length;
  const heading = isImport ? 'Total vontology nodes to import' : 'Total vontology nodes to export';

  const typesHtml = Object.keys(typeCounts).sort().map(t => `${t}: ${typeCounts[t]} nodes`).map(line => `<li>${line}</li>`).join('');

  // For import preview, reconstruct legacy expectation: list of new concept IDs derived from names.
  let newConceptsHtml = '';
  if (isImport) {
    const conceptNames = [];
    const seenConceptSlugs = new Set();
    const slugify = (name) => {
      return '#V#' + name.toLowerCase().replace(/[^a-z0-9\s_\-]/g, '').trim().replace(/\s+/g, '_');
    };
    nodes.forEach(n => {
      if (n && n.type === 'concept' && n.name) {
        const slug = slugify(n.name);
        if (!seenConceptSlugs.has(slug)) {
          seenConceptSlugs.add(slug);
          conceptNames.push({ slug, name: n.name });
        }
      }
    });
    if (conceptNames.length) {
      newConceptsHtml = `
        <div style="margin-top: 10px;">
          <strong>New concepts to be created:</strong>
          <ul style="margin: 5px 0; padding-left: 20px;">
            ${conceptNames.map(c => `<li><code>${c.slug}</code> - ${c.name}</li>`).join('')}
          </ul>
        </div>`;
    }
  }

  const html = `
    <div style="margin-bottom: 10px;">
      <strong>${heading}:</strong> ${total}
    </div>
    ${typesHtml ? `<div><strong>By type:</strong><ul style=\"margin: 5px 0; padding-left: 20px;\">${typesHtml}</ul></div>` : ''}
    ${newConceptsHtml}
  `;

  contentElement.innerHTML = html;
  previewElement.style.display = 'block';
}

// NEW: Render backend analysis response (authoritative normalization + classification)
export function renderVontologyImportAnalysis(analysis) {
  const previewElement = document.getElementById('vontologyImportPreview');
  const contentElement = document.getElementById('vontologyImportPreviewContent');
  if (!previewElement || !contentElement) return;

  const total = analysis.total_nodes || 0;
  const fmt = analysis.format_detected || 'unknown';
  const types = analysis.type_breakdown || {}; // {types, instances, unknown}
  const newCount = (analysis.new_concepts || []).length;
  const existingCount = (analysis.existing_concepts || []).length;
  const cycles = analysis.circular_references || {};
  const hasCycles = cycles.has_cycles;
  const cycleCount = cycles.cycle_count || 0;
  const cycleWarnings = (cycles.warnings || []).slice(0, 3);

  // Derive display names (mirror backend logic for safety) including acronym handling
  const ACRONYMS = new Set(["AI", "NLP", "LLM", "GPU", "CPU", "API", "HTTP", "HTTPS", "JSON", "SQL", "ID", "UUID", "URL", "UI", "UX", "ML", "RL", "DL"]);

  function normalizeWord(w) {
    if (!w) return w;
    if (ACRONYMS.has(w.toUpperCase())) return w.toUpperCase();
    if (w.isUpper && w.length <= 4) return w; // safety, though JS strings don't have isUpper
    return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
  }

  function humanizeConceptId(cid) {
    if (!cid) return '';
    let core = cid.startsWith('#V#') ? cid.slice(3) : cid;
    core = core.replace(/[\-_]+/g, ' ');
    // Split camelCase / PascalCase boundaries
    core = core.replace(/([a-z0-9])([A-Z])/g, '$1 $2');
    return core.split(/\s+/).filter(Boolean).map(part => {
      const upper = part.toUpperCase();
      return ACRONYMS.has(upper) ? upper : (part.charAt(0).toUpperCase() + part.slice(1).toLowerCase());
    }).join(' ');
  }

  function deriveDisplayName(node) {
    if (!node || typeof node !== 'object') return 'Unnamed';
    if (node.name && node.name.trim()) return node.name.trim();
    if (Array.isArray(node.names)) {
      for (const entry of node.names) {
        if (entry && entry.name && entry.name.trim()) return entry.name.trim();
      }
    }
    if (node.label) return humanizeConceptId(node.label);
    const cid = node.concept_id || node.id;
    const humanized = humanizeConceptId(cid || '');
    return humanized || 'Unnamed';
  }

  const sampleNew = (analysis.new_concepts || []).slice(0, 8).map(c => ({ ...c, name: deriveDisplayName(c) }));
  const sampleExisting = (analysis.existing_concepts || []).slice(0, 5).map(c => ({ ...c, name: deriveDisplayName(c) }));

  const typeLines = [];
  if (typeof types.types === 'number') typeLines.push(`types: ${types.types}`);
  if (typeof types.instances === 'number') typeLines.push(`instances: ${types.instances}`);
  if (typeof types.unknown === 'number' && types.unknown > 0) typeLines.push(`unknown: ${types.unknown}`);

  const cycleHtml = hasCycles ? `<div style="margin-top:10px;color:#c62828;"><strong>⚠ Cycles Detected:</strong> ${cycleCount}
    ${cycleWarnings.length ? `<ul style='margin:5px 0 0 18px;'>${cycleWarnings.map(c => `<li>${c}</li>`).join('')}</ul>` : ''}
  </div>` : '<div style="margin-top:10px;color:#2e7d32;"><strong>No circular references detected.</strong></div>';

  contentElement.innerHTML = `
    <div style="margin-bottom:8px;"><strong>Total nodes:</strong> ${total}</div>
    <div style="margin-bottom:8px;"><strong>Detected format:</strong> ${fmt}</div>
    <div style="margin-bottom:8px;"><strong>Type breakdown:</strong> ${typeLines.join(', ') || 'n/a'}</div>
    <div style="margin-bottom:8px;"><strong>Concept actions:</strong> ${newCount} create / ${existingCount} update</div>
    ${sampleNew.length ? `<div style='margin-bottom:8px;'>
      <strong>Sample new concepts:</strong>
  <ul style='margin:4px 0 0 18px;'>${sampleNew.map(c => `<li><code>${c.concept_id}</code> - ${c.name || ''}</li>`).join('')}</ul>
      ${newCount > sampleNew.length ? `<em>… and ${newCount - sampleNew.length} more</em>` : ''}
    </div>`: ''}
    ${sampleExisting.length ? `<div style='margin-bottom:8px;'>
      <strong>Sample existing concepts:</strong>
  <ul style='margin:4px 0 0 18px;'>${sampleExisting.map(c => `<li><code>${c.concept_id}</code> - ${c.name || ''}</li>`).join('')}</ul>
      ${existingCount > sampleExisting.length ? `<em>… and ${existingCount - sampleExisting.length} more</em>` : ''}
    </div>`: ''}
    ${cycleHtml}
    <div style="margin-top:10px;font-size:12px;color:#555;">
      Preview generated via backend normalization. Hierarchy-only imports treat all nodes with parent links as types.
    </div>
  `;
  previewElement.style.display = 'block';
}

// Concepts Export Preview Function
async function exportConceptsPreview(statusElement) {
  updateStatus(statusElement, 'Preparing concepts export preview...', 'info');

  try {
    const response = await fetch('/api/concepts/export-preview');

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const result = await response.json();

    if (result.success) {
      const message = `Preview: Would export ${result.total_count || 0} concepts`;
      updateStatus(statusElement, message, 'success');
      addToHistory('Concepts Export Preview', message, 'success');

      // Display preview details
      const previewElement = document.getElementById('exportPreviewContent');
      if (previewElement) {
        previewElement.innerHTML = `
          <div style="margin-bottom: 10px;">
            <strong>Total concepts to export:</strong> ${result.total_count || 0}
          </div>
          ${result.sample_concepts && result.sample_concepts.length > 0 ?
            `<div>
              <strong>Sample concepts:</strong>
              <ul style="margin: 5px 0; padding-left: 20px; max-height: 150px; overflow-y: auto;">
                ${result.sample_concepts.map(item =>
              `<li>${item.name || 'Unnamed'} (${item.concept_id || 'No concept'})</li>`
            ).join('')}
                ${result.total_count > 10 ? `<li><em>... and ${result.total_count - 10} more</em></li>` : ''}
              </ul>
            </div>` : ''}
        `;
      }
    } else {
      const errorMsg = result.error || 'Failed to generate preview.';
      updateStatus(statusElement, errorMsg, 'error');
      addToHistory('Concepts Export Preview', errorMsg, 'error');
    }
  } catch (error) {
    console.error('Error in Concepts Export Preview:', error);
    updateStatus(statusElement, `Error: ${error.message}`, 'error');
  }
}

// Export the initialization function
export default initializeImportExportTab;
