from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from src.backend.utilities.process_hygiene import terminate_processes_matching_script

def test_terminate_processes_matching_script_terminates_matching_process(tmp_path: Path) -> None:
    target_script = tmp_path / "stale_cleanup_target.py"
    target_script.write_text(
        "import time\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen([sys.executable, str(target_script)])
    try:
        time.sleep(0.5)
        killed = terminate_processes_matching_script(str(target_script), exclude_pid=0)
        assert proc.pid in killed
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
