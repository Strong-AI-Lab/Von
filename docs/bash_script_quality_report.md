# Bash Script Quality Report

**Date**: 2025-11-16  
**Scripts Analyzed**: `setup_all.sh`, `run.sh`

## Summary

Both bash scripts have valid syntax and only minor shellcheck warnings. They are functional but have limited features compared to PowerShell versions.

## Shellcheck Analysis

### setup_all.sh
**Status**: ✅ Valid syntax, 1 warning

**Warnings**:
- Line 163: `VENV_PIP` appears unused (defined but not referenced)
  - **Severity**: Low
  - **Impact**: None (unused variable)
  - **Fix**: Can be removed or used if needed in future

### run.sh
**Status**: ✅ Valid syntax, 7 warnings

**Warnings**:
1. Lines 84, 103, 194, 195, 271, 272: Variable declaration and assignment should be separate
   - **Severity**: Low  
   - **Impact**: Could mask return values in error cases
   - **Example**: `local now_epoch=$(date +%s)` → should be:
     ```bash
     local now_epoch
     now_epoch=$(date +%s)
     ```
   - **Fix**: Split declaration and assignment for better error handling

2. Line 107: `added` variable appears unused
   - **Severity**: Low
   - **Impact**: None (leftover from governance scan parsing)
   - **Fix**: Remove or use in output

## Functional Testing

### Syntax Validation
Both scripts pass bash syntax checking:
```bash
bash -n setup_all.sh  # ✅ No syntax errors
bash -n run.sh        # ✅ No syntax errors
```

### Architecture Review

**setup_all.sh** (252 lines):
- ✅ Self-contained Python and JavaScript setup
- ✅ Python version detection (3.10-3.15)
- ✅ Virtual environment creation
- ✅ PDM installation
- ✅ npm package installation
- ✅ Charset-normalizer fix logic
- ⚠️ No parameter flags support
- ⚠️ No MongoDB installation (manual required)
- ⚠️ No uv/arxiv-mcp-server setup
- ⚠️ No VS Code configuration

**run.sh** (425 lines):
- ✅ Basic process management (PID files)
- ✅ Actions: start, foreground, stop, status, restart, logs, check
- ✅ Graceful shutdown via admin endpoint
- ✅ Admin token generation and persistence
- ✅ Browser auto-open (basic)
- ✅ Basic governance scans
- ✅ Portable timestamp parsing
- ✅ Test DB safety check
- ⚠️ No health checking with retries
- ⚠️ No log rotation
- ⚠️ No daily backups
- ⚠️ No test DB refresh
- ⚠️ No concept data checks
- ⚠️ No legacy text audits
- ⚠️ No preserved fields cleanup
- ⚠️ No relation coverage reporting
- ⚠️ No git auto-update
- ⚠️ Limited command-line flags

## Recommendations

### Immediate (Code Quality)
1. **Fix shellcheck warnings** in run.sh:
   - Separate declaration and assignment for variables
   - Remove unused `added` variable or use it in output

2. **Remove unused variable** in setup_all.sh:
   - Either use `VENV_PIP` or remove the definition

### Short-term (Functionality)
3. **Add missing scripts**:
   - Create `setup_js.sh` matching `setup_js.ps1` functionality
   - Create `setup_py.sh` matching `setup_py.ps1` functionality (minus Windows-specific)

4. **Enhance setup_all.sh**:
   - Add parameter flags: `-skip-py`, `-skip-js`, `-force`, `-ci`
   - Change to orchestrator pattern (call setup_py.sh and setup_js.sh)

5. **Add help command** to run.sh:
   - Document all actions and flags
   - Provide usage examples

### Long-term (Feature Parity)
6. **Port missing features** to run.sh:
   - Health checking with retries
   - Log rotation
   - Daily backups (optional)
   - Test DB refresh (optional)
   - Maintenance tasks (concept checks, audits, cleanup)
   - Additional command-line flags

7. **Testing**:
   - Add automated tests for bash scripts
   - Test on Linux and macOS
   - Verify in clean environments

## Conclusion

The bash scripts are functional and have good code quality with only minor warnings. They provide essential functionality for Linux/macOS users but lack the advanced features of PowerShell versions. The scripts are production-ready for basic use cases but would benefit from:
1. Fixing shellcheck warnings
2. Adding missing setup_js.sh and setup_py.sh scripts
3. Enhancing run.sh with additional features over time

For immediate use, both scripts are safe to use as-is with the limitations documented in the main comparison report.
