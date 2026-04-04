const { spawnSync } = require("node:child_process");
const path = require("node:path");

const REPO_STATIC_JS_PREFIXES = [
    "src/frontend/web/von_interface/static/js/",
    "tests/frontend/",
];

function normaliseCliPath(rawPath) {
    const trimmed = String(rawPath || "").trim();
    if (!trimmed) {
        return "";
    }

    const repoRelativePath = path.isAbsolute(trimmed)
        ? path.relative(process.cwd(), trimmed)
        : trimmed;

    return repoRelativePath.replace(/\\/g, "/").replace(/^\.\/+/, "");
}

function isFrontendStaticJsPath(filePath) {
    return filePath.endsWith(".js")
        && REPO_STATIC_JS_PREFIXES.some((prefix) => filePath.startsWith(prefix));
}

const requestedPaths = process.argv.slice(2).map(normaliseCliPath).filter(Boolean);

if (requestedPaths.length === 0) {
    console.error("Usage: npm run lint:frontend:static -- <changed static-js files>");
    process.exit(2);
}

const lintTargets = [...new Set(requestedPaths.filter(isFrontendStaticJsPath))];

if (lintTargets.length === 0) {
    console.log("No frontend static JS files matched the provided paths.");
    process.exit(0);
}

const eslintBinPath = path.join(process.cwd(), "node_modules", "eslint", "bin", "eslint.js");
const eslintRun = spawnSync(
    process.execPath,
    [eslintBinPath, "--no-error-on-unmatched-pattern", ...lintTargets],
    {
        stdio: "inherit",
    }
);

if (eslintRun.error) {
    console.error(`Failed to run ESLint: ${eslintRun.error.message}`);
    process.exit(1);
}

process.exit(eslintRun.status ?? 1);
