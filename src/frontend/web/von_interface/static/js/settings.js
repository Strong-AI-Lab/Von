import { getJsonDetailed, postJson } from './apiService.js';
import {
  armRetryableLoadState,
  clearRetryableLoadState,
  describeRetryableLoadFailure
} from './utils/retryableLoadState.js';
import { normaliseLocalModelName } from './utils/localModelPreferences.js';

let ollamaHostManagementWritable = false;

export function setOllamaHostManagementWritable(canWrite) {
  ollamaHostManagementWritable = canWrite === true;
  const sharedControlTitle = ollamaHostManagementWritable
    ? 'Updates the shared server Ollama host configuration.'
    : 'Admin or owner privileges are required to update shared Ollama hosts.';
  const input = document.getElementById('newOllamaHostUrl');
  const addButton = document.getElementById('addOllamaHostButton');
  const note = document.getElementById('ollamaHostManagementAccessNote');
  if (input) {
    input.disabled = !ollamaHostManagementWritable;
    input.title = sharedControlTitle;
  }
  if (addButton) {
    addButton.disabled = !ollamaHostManagementWritable;
    addButton.title = sharedControlTitle;
  }
  if (note) {
    note.textContent = ollamaHostManagementWritable
      ? 'Changes here update the shared server host configuration.'
      : 'Read-only: an admin or owner must add, remove, or activate shared Ollama hosts.';
  }
  document.querySelectorAll('[data-ollama-host-write-control="true"]').forEach(button => {
    const alreadyActive = button.dataset.ollamaHostActive === 'true';
    button.disabled = !ollamaHostManagementWritable || alreadyActive;
    button.title = sharedControlTitle;
  });
}

// Settings management functions
export async function loadAvailableModels() {
  const { data } = await getJsonDetailed('/api/settings/models/ollama');
  return data;
}

export async function loadOllamaModelsFromAllHosts(options = {}) {
  const endpoint = options?.bypassCache
    ? '/api/settings/ollama/models?nocache=true'
    : '/api/settings/ollama/models';
  const { data } = await getJsonDetailed(endpoint);
  return data?.models || [];
}

export async function loadOllamaHosts() {
  const { data } = await getJsonDetailed('/api/settings/ollama/hosts');
  return data;
}

export async function saveOllamaHosts(hosts, activeHost = null) {
  if (!ollamaHostManagementWritable) {
    throw new Error('Admin or owner privileges are required to update shared Ollama hosts.');
  }
  try {
    return await postJson('/api/settings/ollama/hosts', {
      hosts: hosts,
      active_host: activeHost
    });
  } catch (err) {
    console.error('Error saving Ollama hosts:', err);
    throw err;
  }
}

export async function verifyOllamaHost(hostUrl) {
  try {
    return await postJson('/api/settings/ollama/verify', {
      host_url: hostUrl
    });
  } catch (err) {
    console.error('Error verifying Ollama host:', err);
    throw err;
  }
}

export async function loadAvailableOpenAIModels() {
  const { data } = await getJsonDetailed('/api/settings/models/openai');
  return data;
}

export async function loadAvailableGeminiModels() {
  const { data } = await getJsonDetailed('/api/settings/models/gemini');
  return data;
}

export async function loadAvailableOpenRouterModels() {
  const { data } = await getJsonDetailed('/api/settings/models/openrouter');
  return data;
}

export async function loadAvailableMetaModels() {
  const { data } = await getJsonDetailed('/api/settings/models/meta');
  return data;
}

function premiumProviderLabel(provider) {
  if (provider === 'gemini') return 'Gemini';
  if (provider === 'openrouter') return 'OpenRouter';
  if (provider === 'meta') return 'Meta Muse';
  return 'OpenAI';
}

function renderPremiumModelSelect(select, provider, models = [], selectedModel = null) {
  if (!select) return;

  const providerLabel = premiumProviderLabel(provider);
  select.innerHTML = `<option value="">Select a ${providerLabel} Model</option>`;

  if (Array.isArray(models) && models.length > 0) {
    models.forEach(rawModelName => {
      const modelName = normaliseLocalModelName(rawModelName);
      if (!modelName) return;

      const option = document.createElement('option');
      option.value = modelName;
      option.textContent = modelName;
      select.appendChild(option);
    });
  } else {
    select.innerHTML = `<option value="">No ${providerLabel} models available</option>`;
  }

  const selectedPremiumModel = normaliseLocalModelName(selectedModel);
  if (selectedPremiumModel) {
    select.value = selectedPremiumModel;
    if (select.value !== selectedPremiumModel) {
      const currentOption = document.createElement('option');
      currentOption.value = selectedPremiumModel;
      currentOption.textContent = `${selectedPremiumModel} (current effective model; unavailable in loaded list)`;
      currentOption.dataset.currentEffective = 'true';
      select.appendChild(currentOption);
      select.value = selectedPremiumModel;
    }
  }
}

export function renderOpenAIModelOptions(selectElementId, models = [], selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;
  renderPremiumModelSelect(select, 'openai', models, selectedModel);
}

export function renderGeminiModelOptions(selectElementId, models = [], selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;
  renderPremiumModelSelect(select, 'gemini', models, selectedModel);
}

export function renderOpenRouterModelOptions(selectElementId, models = [], selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;
  renderPremiumModelSelect(select, 'openrouter', models, selectedModel);
}

export function renderMetaModelOptions(selectElementId, models = [], selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;
  renderPremiumModelSelect(select, 'meta', models, selectedModel);
}

export async function loadAvailableOrganisations() {
  const { data } = await getJsonDetailed('/api/settings/organisations');
  return data;
}

// Settings UI management functions
export function showStatusMessage(elementId, message, isError = false) {
  const element = document.getElementById(elementId);
  if (!element) return;
  
  element.textContent = message;
  element.className = 'status-message';
  if (isError) {
    element.classList.add('error');
  } else {
    element.classList.add('success');
  }
  element.style.display = 'block';
  
  // Auto-hide after 5 seconds
  setTimeout(() => {
    element.style.display = 'none';
  }, 5000);
}

function setSelectSingleOption(select, text) {
  if (!select) return;

  select.innerHTML = '';
  const option = document.createElement('option');
  option.value = '';
  option.textContent = text;
  select.appendChild(option);
}

function clearSelectRetryState(select) {
  clearRetryableLoadState(select);
  if (select) {
    select.title = '';
  }
}

function renderRetryableSelectFailure(select, {
  error,
  fallbackMessage,
  retryAction
}) {
  const failure = describeRetryableLoadFailure(error, fallbackMessage);
  const optionText = failure.retryable
    ? `${failure.message} Click to retry.`
    : failure.message;

  setSelectSingleOption(select, optionText);
  select.title = failure.message;

  if (failure.retryable) {
    armRetryableLoadState(select, retryAction, {
      backgroundDelayMs: Math.max(0, failure.retryAfterSeconds) * 1000
    });
  } else {
    clearRetryableLoadState(select);
  }
}

function renderRetryablePanelFailure(container, {
  error,
  fallbackMessage,
  retryAction,
  buttonLabel
}) {
  if (!container) return;

  const failure = describeRetryableLoadFailure(error, fallbackMessage);
  container.innerHTML = '';
  container.title = failure.message;

  const message = document.createElement('p');
  message.style.color = '#666';
  message.style.fontStyle = 'italic';
  message.textContent = failure.retryable
    ? `${failure.message} Auto-retrying shortly.`
    : failure.message;
  container.appendChild(message);

  if (failure.retryable) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = buttonLabel;
    button.style.padding = '4px 8px';
    button.style.fontSize = '0.85em';
    button.style.marginTop = '6px';
    button.style.background = '#6c757d';
    button.style.color = 'white';
    button.style.border = 'none';
    button.style.borderRadius = '3px';
    button.style.cursor = 'pointer';
    button.addEventListener('click', () => {
      retryAction({ source: 'button' });
    });
    container.appendChild(button);

    armRetryableLoadState(container, retryAction, {
      backgroundDelayMs: Math.max(0, failure.retryAfterSeconds) * 1000
    });
  } else {
    clearRetryableLoadState(container);
  }
}

export async function populateModelDropdown(selectElementId, selectedModel = null, options = {}) {
  const select = document.getElementById(selectElementId);
  if (!select) return [];

  try {
    // Load models from all hosts with host information
    const modelsWithHosts = await loadOllamaModelsFromAllHosts(options);
    select.innerHTML = '<option value="">Select an Ollama Model</option>';

    if (modelsWithHosts?.length > 0) {
      modelsWithHosts.forEach(modelInfo => {
        const option = document.createElement('option');
        option.value = `${modelInfo.host_url}:${modelInfo.name}`; // Store host:model as value
        option.textContent = modelInfo.display_name; // Show "hostname - modelname"
        option.dataset.hostUrl = modelInfo.host_url;
        option.dataset.modelName = modelInfo.name;
        select.appendChild(option);
      });
    } else {
      select.innerHTML = '<option value="">No Ollama models available</option>';
    }

    if (selectedModel) {
      // Try to find exact match first
      select.value = selectedModel;
      
      // If no exact match, try to find by model name only
      if (!select.value && selectedModel) {
        const modelName = selectedModel.includes(':') ? selectedModel.split(':').pop() : selectedModel;
        for (const option of select.options) {
          if (option.dataset.modelName === modelName) {
            select.value = option.value;
            break;
          }
        }
      }
    }
    clearSelectRetryState(select);
    return modelsWithHosts || [];
  } catch (err) {
    console.error('Error populating Ollama model dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'Ollama models are temporarily unavailable.',
      retryAction: () => populateModelDropdown(selectElementId, selectedModel, options)
    });
    return null;
  }
}

export async function populateOpenAIModelDropdown(selectElementId, selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;

  try {
    const models = await loadAvailableOpenAIModels();
    renderPremiumModelSelect(select, 'openai', models, selectedModel);
    clearSelectRetryState(select);
  } catch (err) {
    console.error('Error populating OpenAI model dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'OpenAI models are temporarily unavailable.',
      retryAction: () => populateOpenAIModelDropdown(selectElementId, selectedModel)
    });
  }
}

export async function populateGeminiModelDropdown(selectElementId, selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;

  try {
    const models = await loadAvailableGeminiModels();
    renderPremiumModelSelect(select, 'gemini', models, selectedModel);
    clearSelectRetryState(select);
  } catch (err) {
    console.error('Error populating Gemini model dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'Gemini models are temporarily unavailable.',
      retryAction: () => populateGeminiModelDropdown(selectElementId, selectedModel)
    });
  }
}

export async function populateOpenRouterModelDropdown(selectElementId, selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;

  try {
    const models = await loadAvailableOpenRouterModels();
    renderPremiumModelSelect(select, 'openrouter', models, selectedModel);
    clearSelectRetryState(select);
  } catch (err) {
    console.error('Error populating OpenRouter model dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'OpenRouter models are temporarily unavailable.',
      retryAction: () => populateOpenRouterModelDropdown(selectElementId, selectedModel)
    });
  }
}

export async function populateMetaModelDropdown(selectElementId, selectedModel = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;

  try {
    const models = await loadAvailableMetaModels();
    renderPremiumModelSelect(select, 'meta', models, selectedModel);
    clearSelectRetryState(select);
  } catch (err) {
    console.error('Error populating Meta Muse model dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'Meta Muse models are temporarily unavailable.',
      retryAction: () => populateMetaModelDropdown(selectElementId, selectedModel)
    });
  }
}

export async function populateOrganisationsDropdown(selectElementId, selectedOrgId = null) {
  const select = document.getElementById(selectElementId);
  if (!select) return;

  try {
    const data = await loadAvailableOrganisations();
    select.innerHTML = '<option value="">-- No organisation selected --</option>';

    if (data.organisations?.length > 0) {
      const realOrgs = data.organisations.filter(org => 
        !org.system_tags?.includes('demo_data')
      );

      if (realOrgs.length > 0) {
        realOrgs.forEach(org => {
          const option = document.createElement('option');
          const dbId = org.id || org._id || '';
          const cid  = org.concept_id || '';
          option.value = JSON.stringify({ id: dbId, concept_id: cid });
          option.dataset.id = dbId;
          option.dataset.conceptId = cid;
          // Display name: prefer backend-provided display name.
          option.textContent = org.name || cid || dbId;
          select.appendChild(option);
        });
      } else {
        select.innerHTML = '<option value="">No non-test organisations found</option>';
      }
    } else {
      select.innerHTML = '<option value="">No organisations found</option>';
    }

    if (selectedOrgId) {
      // Options store JSON in value; match using data attribute for DB id
      for (const opt of select.options) {
        if (opt.dataset && opt.dataset.id === String(selectedOrgId)) {
          opt.selected = true;
          break;
        }
      }
    }
    clearSelectRetryState(select);
  } catch (err) {
    console.error('Error populating organisations dropdown:', err);
    renderRetryableSelectFailure(select, {
      error: err,
      fallbackMessage: 'Organisations are temporarily unavailable.',
      retryAction: () => populateOrganisationsDropdown(selectElementId, selectedOrgId)
    });
  }
}

// Ollama hosts management functions
export function renderOllamaHostsList(hosts, activeHost) {
  const hostsList = document.getElementById('ollamaHostsList');
  if (!hostsList) return;
  clearRetryableLoadState(hostsList);
  hostsList.title = '';
  
  if (!hosts || hosts.length === 0) {
    hostsList.innerHTML = '<p style="color: #666; font-style: italic;">No Ollama hosts configured</p>';
    return;
  }
  
  hostsList.innerHTML = hosts.map(host => `
    <div class="ollama-host-item" style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px; padding: 8px; border: 1px solid #ddd; border-radius: 4px; ${host.url === activeHost ? 'background-color: #e7f3ff;' : ''}">
      <span style="flex: 1;">
        <strong>${host.name}</strong> - ${host.url}
        ${host.is_local ? '<span style="color: #28a745; font-size: 0.8em;">(Local)</span>' : '<span style="color: #007cba; font-size: 0.8em;">(Remote)</span>'}
        ${host.url === activeHost ? '<span style="color: #dc3545; font-size: 0.8em; font-weight: bold;">(Active)</span>' : ''}
      </span>
      <button onclick="setActiveOllamaHost('${host.url}')"
              data-ollama-host-write-control="true"
              data-ollama-host-active="${host.url === activeHost ? 'true' : 'false'}"
              style="padding: 4px 8px; font-size: 0.8em; background: #007cba; color: white; border: none; border-radius: 3px; cursor: pointer;"
              disabled>
        ${host.url === activeHost ? 'Active' : 'Set Active'}
      </button>
      <button onclick="testOllamaHost('${host.url}')" 
              style="padding: 4px 8px; font-size: 0.8em; background: #28a745; color: white; border: none; border-radius: 3px; cursor: pointer;">
        Test
      </button>
      <button onclick="removeOllamaHost('${host.url}')"
              data-ollama-host-write-control="true"
              style="padding: 4px 8px; font-size: 0.8em; background: #dc3545; color: white; border: none; border-radius: 3px; cursor: pointer;"
              disabled>
        Remove
      </button>
    </div>
  `).join('');
  setOllamaHostManagementWritable(ollamaHostManagementWritable);
}

export async function loadAndRenderOllamaHosts() {
  const hostsList = document.getElementById('ollamaHostsList');
  try {
    const data = await loadOllamaHosts();
    renderOllamaHostsList(data.hosts, data.active_host);
    return data;
  } catch (err) {
    console.error('Error loading Ollama hosts:', err);
    renderRetryablePanelFailure(hostsList, {
      error: err,
      fallbackMessage: 'Ollama hosts are temporarily unavailable.',
      retryAction: () => loadAndRenderOllamaHosts(),
      buttonLabel: 'Retry host load'
    });
    showStatusMessage(
      'settingsStatusMessage',
      describeRetryableLoadFailure(err, 'Ollama hosts are temporarily unavailable.').message,
      true
    );
    return null;
  }
}

// Global functions for host management (called from HTML buttons)
window.setActiveOllamaHost = async function(hostUrl) {
  if (!ollamaHostManagementWritable) {
    showStatusMessage('settingsStatusMessage', 'Admin or owner privileges are required to update shared Ollama hosts.', true);
    return;
  }
  try {
    const data = await loadOllamaHosts();
    await saveOllamaHosts(data.hosts, hostUrl);
    showStatusMessage('settingsStatusMessage', `Set ${hostUrl} as active host`);
    await loadAndRenderOllamaHosts();
  } catch (err) {
    console.error('Error setting active host:', err);
    showStatusMessage('settingsStatusMessage', 'Error setting active host', true);
  }
};

window.testOllamaHost = async function(hostUrl) {
  try {
    showStatusMessage('settingsStatusMessage', `Testing connection to ${hostUrl}...`);
    const result = await verifyOllamaHost(hostUrl);
    if (result.success) {
      showStatusMessage('settingsStatusMessage', `✓ ${result.message}. Found ${result.models.length} models.`);
    } else {
      showStatusMessage('settingsStatusMessage', `✗ ${result.error}`, true);
    }
  } catch (err) {
    console.error('Error testing host:', err);
    showStatusMessage('settingsStatusMessage', `✗ Failed to connect to ${hostUrl}`, true);
  }
};

window.removeOllamaHost = async function(hostUrl) {
  if (!ollamaHostManagementWritable) {
    showStatusMessage('settingsStatusMessage', 'Admin or owner privileges are required to update shared Ollama hosts.', true);
    return;
  }
  try {
    const data = await loadOllamaHosts();
    const updatedHosts = data.hosts.filter(host => host.url !== hostUrl);
    let newActiveHost = data.active_host;
    
    // If we're removing the active host, set a new one
    if (data.active_host === hostUrl && updatedHosts.length > 0) {
      newActiveHost = updatedHosts[0].url;
    } else if (updatedHosts.length === 0) {
      newActiveHost = null;
    }
    
    await saveOllamaHosts(updatedHosts, newActiveHost);
    showStatusMessage('settingsStatusMessage', `Removed host ${hostUrl}`);
    await loadAndRenderOllamaHosts();
  } catch (err) {
    console.error('Error removing host:', err);
    showStatusMessage('settingsStatusMessage', 'Error removing host', true);
  }
};
