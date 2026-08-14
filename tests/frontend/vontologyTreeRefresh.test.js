const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(
  path.join(
    __dirname,
    '../../src/frontend/web/von_interface/static/js/vontology.js',
  ),
  'utf8',
);

describe('Vontology tree refresh cache bypass', () => {
  beforeEach(() => {
    jest.resetModules();
  });

  test('manual refresh requests a fresh asynchronous tree build', () => {
    expect(source).toContain(
      'fetchAndRenderVontologyTree({ forceRefresh = false } = {})',
    );
    expect(source).toContain(
      "'/vontology/api/vontology/tree_async?refresh=1'",
    );
    expect(source).toMatch(
      /handleRefreshTree[\s\S]*fetchAndRenderVontologyTree\(\{ forceRefresh: true \}\)/,
    );
    expect(source).toContain('if (!forceRefresh && __vontologyPreloadInFlight)');
    expect(source).toMatch(/if \(forceRefresh\) \{\s*retireVontologyPreload\(\);/);
  });

  test('retired preload data cannot overwrite a later explicit refresh', () => {
    const vontology = require(
      '../../src/frontend/web/von_interface/static/js/vontology.js'
    );
    const initial = vontology.__test_getVontologyPreloadState();
    const oldTree = { tree: [{ id: '#V#old' }] };

    expect(
      vontology.__test_publishVontologyPreload(
        initial.generation,
        oldTree,
        { '#V#old': { entity_count: 1 } },
      ),
    ).toBe(true);
    vontology.__test_retireVontologyPreload();

    const retired = vontology.__test_getVontologyPreloadState();
    expect(retired.tree).toBeNull();
    expect(retired.entityCounts).toBeNull();
    expect(retired.generation).toBe(initial.generation + 1);
    expect(
      vontology.__test_publishVontologyPreload(
        initial.generation,
        oldTree,
        null,
      ),
    ).toBe(false);
    expect(vontology.__test_getVontologyPreloadState().tree).toBeNull();
  });
});
