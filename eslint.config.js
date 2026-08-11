const js = require("@eslint/js");
const globals = require("globals");

module.exports = [
    js.configs.recommended,
    {
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "module",
            globals: {
                ...globals.browser,
                ...globals.es2021,
            }
        },
        rules: {
            // Relaxed rules for existing codebase
            "no-unused-vars": ["warn", {
                "argsIgnorePattern": "^_",
                "varsIgnorePattern": "^_",
                "caughtErrorsIgnorePattern": "^_"
            }],
            "no-undef": "error",
            "no-constant-condition": "warn",
            "no-empty": ["warn", { "allowEmptyCatch": true }],
            "no-prototype-builtins": "off",
            "no-useless-escape": "warn",
        }
    },
    {
        // Node.js files (config, tests)
        files: ["*.config.js", "jest.*.js", "babel.config.js", "scripts/**/*.cjs"],
        languageOptions: {
            globals: {
                ...globals.node,
            }
        }
    },
    {
        // Test files
        files: [
            "tests/**/*.js",
            "src/**/*.test.js",
            "src/**/test/**/*.js",
        ],
        languageOptions: {
            globals: {
                ...globals.node,
                ...globals.jest,
            }
        }
    },
    {
        // Ignore patterns
        ignores: [
            "node_modules/**",
            ".venv/**",
            "coverage/**",
            "**/*.min.js",
            "src/frontend/web/von_interface/static/js/lib/**",
        ]
    }
];
