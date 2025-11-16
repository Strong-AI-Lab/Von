# Bash and PowerShell Script Comparison Report

**Date**: 2025-11-16  
**Issue**: Check that bash setup and running scripts match the PowerShell (.ps1) versions

## Executive Summary

The PowerShell scripts (`*.ps1`) are significantly more feature-rich and maintained compared to their bash counterparts (`*.sh`). Key findings:

1. **Missing Scripts**: `setup_js.sh` and `setup_py.sh` do not exist
2. **Architecture Mismatch**: Different design patterns between setup scripts
3. **Feature Gap**: `run.sh` lacks many features present in `run.ps1`
4. **Documentation Gap**: Official documentation only references PowerShell scripts

---

## Detailed Comparison

### 1. Setup Scripts

#### 1.1 `setup_all.ps1` vs `setup_all.sh`

| Aspect | setup_all.ps1 | setup_all.sh |
|--------|---------------|--------------|
| **Lines of Code** | 118 | 252 |
| **Architecture** | Orchestrator (delegates to setup_py.ps1 and setup_js.ps1) | Self-contained (all logic inline) |
| **Parameters** | ✅ Extensive: -SkipPy, -SkipJS, -Force, -CI, -ConfigureVSCode, -PythonVersion, -SkipMongoDB | ❌ None |
| **Exit Codes** | ✅ 0=success, non-zero=failure | ✅ 0=success, non-zero=failure |
| **Timing/Duration** | ✅ Tracks and reports duration | ❌ No timing |
| **Error Handling** | ✅ Try/catch with detailed messages | ⚠️ Basic with `set -e` |
| **Version Info** | Not specified | V3.0.1 |

**Key Functional Differences**:
- **PS1**: Modular design allows selective Python or JS setup with fine-grained control
- **Bash**: Monolithic approach with embedded Python version detection and charset-normalizer fixes
- **PS1**: Better suited for CI/CD with its parameter flags
- **Bash**: More portable but less flexible

#### 1.2 `setup_js.ps1` - **NO BASH EQUIVALENT**

**Features in PowerShell version** (77 lines):
- ✅ Validates Node.js and npm presence
- ✅ Conditional install (skips if `node_modules` exists unless `-Force`)
- ✅ `-Force` flag for forced reinstall
- ✅ `-CI` flag for quieter output (`--no-fund`, `--no-audit`)
- ✅ `-NoJestCheck` flag to skip Jest verification
- ✅ Jest version reporting after install
- ✅ Detailed error messages

**Status**: ❌ **MISSING**

**Recommendation**: Create `setup_js.sh` with equivalent functionality.

#### 1.3 `setup_py.ps1` - **NO BASH EQUIVALENT**

**Features in PowerShell version** (598 lines):
- ✅ Python version detection and selection (3.10-3.15)
- ✅ Virtual environment creation
- ✅ PDM installation and configuration
- ✅ MongoDB installation (Windows-specific with MSI installer)
- ✅ uv and arxiv-mcp-server installation
- ✅ .env file creation from template
- ✅ VS Code settings configuration
- ✅ `-ConfigureVSCode` flag
- ✅ `-Reset` flag for clean environment
- ✅ `-PythonVersion` flag for version selection
- ✅ `-SkipMongoDB` flag

**Status**: ❌ **MISSING**

**Recommendation**: Create `setup_py.sh` with equivalent functionality (excluding Windows-specific MongoDB installer).

**Note**: Much of this functionality is embedded in `setup_all.sh`, but extracting it to a separate script would match the PowerShell architecture.

---

### 2. Runtime Scripts

#### 2.1 `run.ps1` vs `run.sh`

| Feature Category | run.ps1 | run.sh | Notes |
|-----------------|---------|--------|-------|
| **Lines of Code** | 1570 | 425 | PS1 is 3.7x larger |
| **Basic Actions** | start, foreground, stop, status, restart, logs, check, autoupdate, help | start, foreground, stop, status, restart, logs, check | Missing: autoupdate, help |
| **Process Management** | ✅ Advanced (PID files, health checks, port listening) | ⚠️ Basic (PID files only) | |
| **Health Checking** | ✅ Advanced with retry logic and grace periods | ❌ None | PS1 has configurable timeouts |
| **Log Management** | ✅ Rotation, timestamping, retention policy | ⚠️ Basic timestamping | PS1 keeps last N logs |
| **Browser Control** | ✅ Chrome preference, single-tab behaviour, sentinel file | ⚠️ Basic auto-open | PS1 tracks first-open state |
| **Admin Token** | ✅ Generation and persistence | ✅ Generation and persistence | Both implement this |
| **Graceful Shutdown** | ✅ Via /admin/shutdown endpoint | ✅ Via /admin/shutdown endpoint | Both implement this |
| **Daily Backups** | ✅ Automated background jobs | ❌ Not implemented | PS1 has configurable intervals |
| **Test DB Refresh** | ✅ Automated periodic refresh | ❌ Not implemented | PS1 clones prod to test |
| **Governance Scans** | ✅ Daily concept tagging | ⚠️ Basic implementation | PS1 has retry/backoff logic |
| **Data Absence Checks** | ✅ Periodic validation | ❌ Not implemented | PS1 ensures migrations |
| **Legacy Text Audits** | ✅ Residual field checking | ❌ Not implemented | PS1 detects violations |
| **Preserved Fields Cleanup** | ✅ Automated migration | ❌ Not implemented | PS1 has dry-run mode |
| **Relation Coverage** | ✅ Summary reporting | ❌ Not implemented | PS1 shows predicate stats |
| **Backup Migration** | ✅ W: drive migration logic | ❌ Not implemented | PS1-specific feature |
| **Git Auto-Update** | ✅ Continuous pull/restart | ❌ Not implemented | PS1 autoupdate action |
| **Multi-Instance** | ⚠️ Partial (port-based naming) | ❌ Not implemented | Reserved for future |
| **PID Synchronisation** | ✅ Detects and fixes mismatches | ❌ Not implemented | PS1 syncs with port owner |
| **Test DB Safety Check** | ✅ Blocks server start with test DB | ✅ Blocks server start with test DB | Both implement this |

**Parameters/Flags**:

| Flag | run.ps1 | run.sh |
|------|---------|--------|
| `-Port` | ✅ | ⚠️ Via `PORT` env var |
| `-NoBrowser` | ✅ | ✅ `--no-browser` |
| `-ForceBrowser` | ✅ | ✅ `--force-browser` |
| `-Tail` | ✅ | ❌ |
| `-Follow` | ✅ | ⚠️ Hardcoded in logs action |
| `-LogRetention` | ✅ | ❌ |
| `-AdminToken` | ✅ | ❌ |
| `-SkipHealth` | ✅ | ❌ |
| `-HealthTimeoutSec` | ✅ | ❌ |
| `-HealthGraceSec` | ✅ | ❌ |
| `-ReadyLogPatterns` | ✅ | ❌ |
| `-DisableLogReady` | ✅ | ❌ |
| `-HealthDebug` | ✅ | ❌ |
| `-ShowRelationCoverage` | ✅ | ❌ |
| `-UpdateIntervalMinutes` | ✅ | ❌ |
| `-UpdateBranch` | ✅ | ❌ |
| `-UpdateNoRestartIfRunning` | ✅ | ❌ |
| `-NoBackupMigrate` | ✅ | ❌ |

---

## Impact Analysis

### For Windows Users (PowerShell Primary)
- ✅ **Full-featured environment** with all automation and monitoring
- ✅ Production-ready with backup, health checks, and governance
- ✅ Well-documented in README and CONTRIBUTING

### For Linux/macOS Users (Bash Primary)
- ⚠️ **Limited setup flexibility** - no modular scripts
- ⚠️ **Missing automation** - no backups, test DB refresh, or maintenance tasks
- ⚠️ **Basic monitoring** - no health checks or advanced logging
- ⚠️ **Undocumented** - not mentioned in official docs
- ⚠️ **Manual intervention required** for tasks automated on Windows

### Cross-Platform Development
- ❌ **Inconsistent experience** across operating systems
- ❌ **Feature parity gaps** may lead to platform-specific bugs
- ❌ **CI/CD complexity** - different automation capabilities per platform

---

## Recommendations

### Priority 1: Critical Gaps

1. **Create `setup_js.sh`**
   - Port functionality from `setup_js.ps1`
   - Ensure parameter flag compatibility
   - Add Jest verification

2. **Create `setup_py.sh`**
   - Port non-Windows functionality from `setup_py.ps1`
   - Implement Python version selection
   - Add .env template handling
   - Skip Windows-specific MongoDB installer (document manual install)

3. **Refactor `setup_all.sh`**
   - Change to orchestrator pattern (call `setup_py.sh` and `setup_js.sh`)
   - Add parameter flags matching PS1 version
   - Maintain backward compatibility

### Priority 2: Feature Parity

4. **Enhance `run.sh`**
   - Add log rotation functionality
   - Implement advanced health checking with retries
   - Add help command
   - Port maintenance tasks:
     - Daily backups
     - Test DB refresh
     - Data absence checks
     - Legacy text audits
     - Preserved fields cleanup
     - Relation coverage reporting

5. **Add Missing Flags**
   - `-Tail`, `-Follow`, `-LogRetention`
   - Health check configuration flags
   - Git auto-update parameters

### Priority 3: Documentation

6. **Update Official Documentation**
   - Add Linux/macOS setup instructions to README
   - Document bash script usage in CONTRIBUTING
   - Note feature differences between platforms
   - Provide workarounds for missing features

7. **Add Script Help Text**
   - Embed usage examples in bash scripts
   - Document all flags and environment variables
   - Include troubleshooting tips

### Priority 4: Quality Improvements

8. **Testing**
   - Create test suite for both PowerShell and Bash scripts
   - Verify cross-platform compatibility
   - Validate parameter handling

9. **CI/CD Integration**
   - Add automated script testing to GitHub Actions
   - Test on Windows, Linux, and macOS
   - Verify setup scripts in clean environments

---

## Implementation Notes

### Platform-Specific Considerations

**Bash scripts should:**
- Use portable POSIX-compliant syntax where possible
- Detect OS (Linux vs macOS) for platform-specific behaviour
- Handle missing commands gracefully (e.g., `gdate` vs `date`)
- Provide clear error messages for unsupported features

**Windows-specific features to exclude from Bash:**
- W: drive backup migration
- MSI-based MongoDB installer
- Windows PowerShell registry access
- .NET-specific functionality

### Maintaining Parity

**Version Control:**
- Keep script feature sets documented
- Update both versions when adding new functionality
- Use feature flags for platform-specific code

**Testing Strategy:**
- Automated tests on GitHub Actions (Ubuntu, macOS, Windows)
- Manual verification of production deployments
- User acceptance testing across platforms

---

## Conclusion

The PowerShell scripts represent a mature, production-ready automation suite, while the Bash scripts provide only basic functionality. To ensure a consistent cross-platform experience, the Bash scripts should be brought up to feature parity with their PowerShell counterparts, excluding Windows-specific functionality.

**Immediate Action Items:**
1. Create `setup_js.sh` and `setup_py.sh`
2. Document current script limitations
3. Plan incremental enhancement of `run.sh`
4. Update user-facing documentation

**Long-term Goals:**
- Full feature parity between PowerShell and Bash
- Comprehensive test coverage
- Unified documentation
- Consistent cross-platform experience
