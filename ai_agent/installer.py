import stat
import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog


def install_hook(script_path: Path) -> None:
    root = tk.Tk()
    root.withdraw()
    target_dir = filedialog.askdirectory(
        title="Seleziona la root del repository Git da controllare"
    )

    if not target_dir:
        sys.exit(1)

    hooks_dir = Path(target_dir) / ".git" / "hooks"
    if not hooks_dir.exists():
        print("Errore: directory .git/hooks non trovata.")
        sys.exit(1)

    pre_push_path = hooks_dir / "pre-push"
    script_path = script_path.resolve().as_posix()
    python_exe = Path(sys.executable).resolve().as_posix()

    bash_hook = f'#!/bin/sh\n"{python_exe}" "{script_path}"\nexit $?\n'

    try:
        pre_push_path.write_text(bash_hook, encoding="utf-8")
        current_mode = pre_push_path.stat().st_mode
        pre_push_path.chmod(current_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        print("Hook pre-push installato correttamente.")
        print(f"Percorso: {pre_push_path}")
        print("")
        print("Configurazione API key:")
        print("  export GOOGLE_API_KEY=la_tua_chiave")
        print("  oppure crea un file .api_key nella root del repository")
        print("  e aggiungi .api_key al .gitignore.")
    except Exception as exc:
        print(f"Installazione hook non riuscita: {exc}")
        sys.exit(1)

    sys.exit(0)
