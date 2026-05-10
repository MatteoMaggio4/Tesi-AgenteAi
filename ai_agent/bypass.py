import os
import time

from .config import BYPASS_ENV_VAR, BYPASS_TTL_SECONDS
from .git_utils import get_git_dir


def check_and_clear_bypass() -> bool:
    """
    Consuma un eventuale flag di bypass scritto nella directory .git.

    Il timestamp evita che un bypass rimasto sul disco per errore faccia saltare
    controlli futuri in modo silenzioso.
    """
    git_dir = get_git_dir()
    if git_dir:
        bypass_file = git_dir / "ai_agent_bypass"
        if bypass_file.exists():
            try:
                written_at = float(bypass_file.read_text(encoding="utf-8").strip())
                bypass_file.unlink(missing_ok=True)

                age = time.time() - written_at
                if age <= BYPASS_TTL_SECONDS:
                    return True

                print(
                    "[Agente AI] Bypass ignorato perche scaduto "
                    f"({int(age)}s > {BYPASS_TTL_SECONDS}s)."
                )
            except Exception:
                try:
                    bypass_file.unlink(missing_ok=True)
                except Exception:
                    pass

    if os.environ.get(BYPASS_ENV_VAR) == "1":
        return True

    return False


def set_bypass_flag() -> None:
    """Scrive il flag usato per evitare il loop dopo un commit automatico."""
    git_dir = get_git_dir()
    if not git_dir:
        return
    try:
        (git_dir / "ai_agent_bypass").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
