import {
  buildDraftSource,
  buildPolicyEditors,
  buildWorkflowStudioRequestHeaders,
  buildWorkflowLayout,
  normaliseAuthoringSpecForEditor,
  summarisePreviewDiff
} from '../workflowStudioPage.js';

describe('workflowStudioPage helpers', () => {
  test('normalises authoring spec fields for bounded editing', () => {
    const result = normaliseAuthoringSpecForEditor(
      {
        description: 'Demo workflow',
        initial_state_key: 'start',
        workflow_metadata: { background_launch_policy: { mode: 'manual' } },
        steps: [
          {
            state_id: 'start',
            reads_variables: ['alpha', 'alpha', 'beta'],
            writes_context_keys: ['result', 'result']
          }
        ]
      },
      '#V#demo_workflow'
    );

    expect(result.workflow_id).toBe('#V#demo_workflow');
    expect(result.description).toBe('Demo workflow');
    expect(result.steps[0].reads_variables).toEqual(['alpha', 'beta']);
    expect(result.steps[0].writes_context_keys).toEqual(['result']);
    expect(result.workflow_metadata).toEqual({
      background_launch_policy: { mode: 'manual' }
    });
  });

  test('builds a deterministic layout for workflow topology rendering', () => {
    const layout = buildWorkflowLayout({
      initial_step: 'start',
      steps: [
        { step_id: 'start', name: 'Start' },
        { step_id: 'middle', name: 'Middle' },
        { step_id: 'done', name: 'Done' }
      ],
      edges: [
        { from: 'start', to: 'middle', predicate: 'nextStep' },
        { from: 'middle', to: 'done', predicate: 'nextStep' }
      ]
    });

    expect(layout.nodes).toHaveLength(3);
    expect(layout.edges).toHaveLength(2);
    expect(layout.nodes[0].stepId).toBe('start');
    expect(layout.width).toBeGreaterThan(700);
    expect(layout.height).toBeGreaterThanOrEqual(Math.max(...layout.nodes.map(node => node.y + node.height)));
  });

  test('summarises preview diffs compactly', () => {
    expect(
      summarisePreviewDiff({
        workflow_description_changed: true,
        initial_state_changed: false,
        changed_state_count: 2
      })
    ).toContain('description');
  });

  test('adds the window-session header to studio API requests', () => {
    sessionStorage.setItem('von_window_session_id', 'ws_test123');

    const getHeaders = buildWorkflowStudioRequestHeaders();
    const postHeaders = buildWorkflowStudioRequestHeaders({ body: '{}' });

    expect(getHeaders.Accept).toBe('application/json');
    expect(getHeaders['Content-Type']).toBeUndefined();
    expect(getHeaders['X-Von-Window-Session']).toBe('ws_test123');
    expect(postHeaders['Content-Type']).toBe('application/json');
    expect(postHeaders['X-Von-Window-Session']).toBe('ws_test123');
  });

  test('prefers a pending proposal draft over the authoritative current spec', () => {
    const result = buildDraftSource({
      authoring: {
        current_spec: { workflow_id: '#V#alpha_workflow', description: 'Current' }
      },
      proposal: {
        active: true,
        authoring_spec: { workflow_id: '#V#alpha_workflow', description: 'Pending' }
      }
    });

    expect(result.label).toBe('pending proposal');
    expect(result.spec.description).toBe('Pending');
  });

  test('serialises workflow policy metadata into stable editor JSON', () => {
    const editors = buildPolicyEditors({
      workflow_metadata: {
        routing_profile: { role: 'authoring' },
        event_bindings: [{ event_type: 'concept.created' }]
      }
    });

    expect(editors.routing_profile).toContain('"role": "authoring"');
    expect(editors.event_bindings).toContain('"event_type": "concept.created"');
    expect(editors.schedule_specs).toBe('');
  });
});


describe('actor schedule operations', () => {
  const flush = async () => { for (let i = 0; i < 60; i += 1) await Promise.resolve(); };
  test('creates, reuses a retry key, pauses and reloads a canonical schedule', async () => {
    const api = require('../apiService.js');
    jest.spyOn(api, 'ensureUniqueWindowSessionId').mockResolvedValue(undefined);
    document.body.innerHTML = `
      <div id="workflowStudioTitle"></div><div id="workflowStudioSummary"></div><div id="workflowStudioSummaryChips"></div><div id="workflowStudioCatalogue"></div>
      <div id="workflowStudioCanvas"></div>
      <div id="workflowStudioStatusBanner"></div>
      <button class="workflow-studio-view-tab" data-view="operations">Operations</button>`;
    let schedule = null;
    let firstCreate = true;
    let resolveOther;
    const otherRequests = [];
    const creations = [];
    global.fetch = jest.fn(async (url, options = {}) => {
      const reply = data => ({ ok: true, json: async () => data });
      if (String(url).startsWith('/api/workflow-studio/catalogue')) return reply({ items: [{ workflow_id: '#V#schedule_ui_test', is_executable: true }, { workflow_id: '#V#other_workflow', is_executable: false, executability_reason: 'inspection_summary_pending' }] });
      if (String(url).endsWith(encodeURIComponent('#V#other_workflow'))) return new Promise(resolve => {
        resolveOther = (summary = { is_executable: true, executability_reason: 'executable', description: 'Loaded description' }) => resolve(reply({ summary, operations: { schedules: { items: [] } } }));
        otherRequests.push(resolveOther);
      });
      if (String(url).startsWith('/api/workflow-studio/workflows/')) return reply({
        summary: { is_executable: true }, operations: { schedules: { items: schedule ? [schedule] : [] } }
      });
      if (url === '/api/workflows/schedules') {
        creations.push(JSON.parse(options.body));
        // Save before losing the response, as an ordinary network retry can.
        schedule = { schedule_id: '#V#schedule_saved', workflow_id: '#V#schedule_ui_test', schedule_type: 'interval', origin: 'actor_owned', enabled: true, next_run_at: '2026-09-06T12:00:00Z' };
        if (firstCreate) { firstCreate = false; throw new TypeError('Failed to fetch'); }
        return reply({ ...schedule, success: true, idempotent_replay: true });
      }
      if (String(url).startsWith('/api/workflows/schedules?')) return reply({ items: schedule ? [schedule] : [], count: schedule ? 1 : 0 });
      if (String(url).endsWith('/enabled')) {
        schedule.enabled = JSON.parse(options.body).enabled;
        return reply({ ...schedule, success: true });
      }
      return reply({});
    });
    try {
      document.dispatchEvent(new Event('DOMContentLoaded'));
      await flush();
      document.querySelector('[data-view="operations"]').click();
      document.querySelector('[data-action="create-schedule"]').click();
      await flush();
      expect(document.getElementById('workflowStudioStatusBanner').textContent).toContain('Failed to fetch');
      document.querySelector('[data-action="create-schedule"]').click();
      await flush();
      expect(creations).toHaveLength(2);
      expect(creations[0].idempotency_key).toBe(creations[1].idempotency_key);
      expect(creations[0]).not.toHaveProperty('user_id');
      expect(document.getElementById('workflowStudioCanvas').textContent).toContain('Existing schedule reused');
      document.querySelector('[data-action="toggle-schedule"]').click();
      await flush();
      expect(schedule.enabled).toBe(false);
      document.querySelector('[data-action="reload-schedules"]').click();
      await flush();
      expect(document.getElementById('workflowStudioCanvas').textContent).toContain('Paused');
      expect(document.querySelector('[data-action="toggle-schedule"]').textContent).toBe('Resume');
      expect(global.fetch.mock.calls.filter(([url]) => String(url).startsWith('/api/workflow-studio/workflows/'))).toHaveLength(1);
      expect(document.querySelector('[data-workflow-id="#V#other_workflow"]').textContent).toContain('Not yet verified');
      document.querySelector('[data-workflow-id="#V#other_workflow"]').click();
      await flush();
      expect(document.getElementById('workflowStudioTitle').textContent).toBe('#V#other_workflow');
      expect(document.getElementById('workflowStudioCanvas').textContent).toContain('Loading workflow');
      expect(document.getElementById('workflowStudioCanvas').getAttribute('aria-busy')).toBe('true');
      expect(document.querySelector('[data-action="create-schedule"]')).toBeNull();
      expect(document.querySelector('[data-action="toggle-schedule"]')).toBeNull();
      document.querySelector('[data-workflow-id="#V#schedule_ui_test"]').click();
      await flush();
      resolveOther();
      await flush();
      expect(document.querySelector('[data-action="toggle-schedule"]').dataset.scheduleId).toBe('#V#schedule_saved');
      // A -> B -> A: an earlier A response must not overwrite the newer A.
      document.querySelector('[data-workflow-id="#V#other_workflow"]').click();
      await flush();
      document.querySelector('[data-workflow-id="#V#schedule_ui_test"]').click();
      await flush();
      document.querySelector('[data-workflow-id="#V#other_workflow"]').click();
      await flush();
      otherRequests[2]();
      await flush();
      otherRequests[1]({ description: 'Stale description', is_executable: false });
      await flush();
      expect(document.getElementById('workflowStudioSummary').textContent).toBe('Loaded description');
      const loadedCard = document.querySelector('[data-workflow-id="#V#other_workflow"]');
      expect(loadedCard.textContent).toContain('Loaded description');
      expect(loadedCard.textContent).toContain('Executable');
      expect(loadedCard.getAttribute('aria-pressed')).toBe('true');
      global.fetch.mockRejectedValueOnce(new Error('Temporary read failure'));
      const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
      loadedCard.click();
      await flush();
      expect(document.getElementById('workflowStudioCanvas').getAttribute('aria-busy')).toBe('false');
      expect(document.getElementById('workflowStudioCanvas').textContent).toContain('Could not load workflow');
      document.querySelector('[data-action="retry-detail"]').click();
      await flush();
      resolveOther();
      await flush();
      expect(document.getElementById('workflowStudioSummary').textContent).toBe('Loaded description');
      consoleError.mockRestore();
    } finally {
      jest.restoreAllMocks();
      delete global.fetch;
    }
  });
});
