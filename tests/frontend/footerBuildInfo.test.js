const fs = require('fs');
const path = require('path');

const template = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/von_interface.html'),
    'utf8'
);
const mainSource = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/js/main.js'),
    'utf8'
);
const styles = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/static/styles.css'),
    'utf8'
);

describe('footer build information', () => {
    test('places compact build information immediately after uptime', () => {
        const uptimeFooter = template.match(/<p id="serverUptimeFooter"[\s\S]*?<\/p>/)?.[0] || '';

        expect(uptimeFooter.indexOf('id="serverUptimeValue"')).toBeGreaterThanOrEqual(0);
        expect(uptimeFooter.indexOf('id="serverBuildInfo"'))
            .toBeGreaterThan(uptimeFooter.indexOf('id="serverUptimeValue"'));
        expect(mainSource).toContain('versionDetails.git_short_commit');
        expect(mainSource).toContain('versionDetails.git_commit_timestamp');
        expect(mainSource).toContain("shortCommit.slice(0, 8)");
    });

    test('hides build information, but not uptime, below the footer crowding threshold', () => {
        expect(styles).toMatch(
            /@media \(max-width:\s*760px\)\s*\{\s*\.server-build-info\s*\{\s*display:\s*none;/
        );
        expect(styles).not.toMatch(
            /@media \(max-width:\s*760px\)[\s\S]*?#serverUptimeFooter\s*\{\s*display:\s*none;/
        );
    });
});
