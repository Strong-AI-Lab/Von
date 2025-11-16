# Documentation

This directory contains technical documentation for the Von project.

## Bash and PowerShell Script Compatibility

**Quick Links:**
- **[BASH_POWERSHELL_COMPATIBILITY_SUMMARY.md](BASH_POWERSHELL_COMPATIBILITY_SUMMARY.md)** - Executive summary and final report
- **[script_comparison_report.md](script_comparison_report.md)** - Detailed feature-by-feature comparison
- **[bash_script_quality_report.md](bash_script_quality_report.md)** - Code quality and shellcheck analysis

### Summary

The PowerShell scripts (`.ps1`) are significantly more feature-rich than their Bash counterparts (`.sh`):

- **Missing Scripts**: `setup_js.sh` and `setup_py.sh` do not exist
- **Feature Gap**: `run.sh` (425 lines) vs `run.ps1` (1570 lines) - Bash version lacks:
  - Daily backups
  - Test DB refresh  
  - Advanced health checking
  - Log rotation
  - Governance automation
  - Git auto-update
  - 17+ command-line flags

- **Architecture Difference**: 
  - PowerShell uses modular orchestrator pattern
  - Bash uses monolithic self-contained approach

### For Users

- **Windows users**: Use PowerShell scripts for full functionality
- **Linux/macOS users**: Current Bash scripts provide basic setup and runtime management
  - Advanced features require manual intervention
  - See the comparison report for details on missing features

### For Developers

See the full [script_comparison_report.md](script_comparison_report.md) for:
- Detailed feature comparisons
- Implementation recommendations  
- Platform-specific considerations
- Testing and CI/CD guidance
