import sys
from pathlib import Path

from .app import GitAgentApp
from .bypass import check_and_clear_bypass
from .installer import install_hook


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_hook(Path(sys.argv[0]))
        return 0

    if check_and_clear_bypass():
        print("[Agente AI] Bypass temporaneo riconosciuto. Push autorizzato.")
        return 0

    pre_push_stdin = ""
    try:
        if not sys.stdin.closed:
            pre_push_stdin = sys.stdin.read()
    except Exception:
        pre_push_stdin = ""

    app = GitAgentApp(pre_push_stdin)
    app.mainloop()
    return app.exit_code
