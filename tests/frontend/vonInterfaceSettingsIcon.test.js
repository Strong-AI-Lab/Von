const fs = require('fs');
const path = require('path');

describe('Von interface settings tab icon', () => {
    it('uses a stroke-based gear icon instead of the old filled silhouette path', () => {
        const templatePath = path.join(
            __dirname,
            '../../src/frontend/web/von_interface/templates/von_interface.html'
        );
        const template = fs.readFileSync(templatePath, 'utf8');
        const settingsSvgMatch = template.match(
            /<div class="tab-button settings-tab-button"[\s\S]*?<svg[\s\S]*?<\/svg>/
        );

        expect(settingsSvgMatch).not.toBeNull();

        const settingsSvg = settingsSvgMatch[0];
        expect(settingsSvg).toContain('stroke-linecap="round"');
        expect(settingsSvg).toContain('stroke-linejoin="round"');
        expect(settingsSvg).toContain('a1.65 1.65 0 0 0 .33 1.82');
        expect(settingsSvg).not.toContain('a7.7 7.7 0 0 0 .1-2');
    });
});
