import subprocess
from pathlib import Path
from typing import List, Optional


def run_process(
    args: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Esegue un comando senza shell, cosi da ridurre il rischio di injection."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
