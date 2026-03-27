import {
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
    expect(layout.height).toBeGreaterThan(300);
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
});
