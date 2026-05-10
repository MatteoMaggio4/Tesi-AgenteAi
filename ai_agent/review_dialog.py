import difflib
import shutil
import time

import tkinter as tk

import customtkinter as ctk

from .config import DIFF_CONTEXT_LINES
from .git_manager import GitManager


class ReviewDialogMixin:
    def show_diff_viewer(self) -> None:
        try:
            self._show_diff_viewer_impl()
        except Exception as exc:
            self.safe_log(f"[!] Impossibile aprire la revisione visuale: {exc}")
            with self._lock:
                self.action_taken = "Errore popup, file saltato"
            self._decision_event.set()

    def _show_diff_viewer_impl(self) -> None:
        if not self.target_file:
            self._decision_event.set()
            return

        old_text = GitManager._read_text_with_fallback(self.target_file) or ""
        old_lines = old_text.splitlines(keepends=True)

        with self._lock:
            fixed_code = self.fixed_code
            t_passed = self.tests_passed
            t_failed = self.tests_failed
            t_status = self.test_status
            t_log = self.test_output_log
            test_code = self.generated_test_code

        has_patch = bool(fixed_code) and fixed_code != old_text
        self._current_has_patch = has_patch
        if not fixed_code:
            fixed_code = old_text

        new_lines = fixed_code.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="Originale",
                tofile="Patch AI",
                n=DIFF_CONTEXT_LINES,
            )
        )

        popup = ctk.CTkToplevel(self)
        popup.title(f"Revisione file: {self.target_file.name}")
        popup.geometry("940x820")
        popup.transient(self)
        popup.grab_set()
        popup.protocol("WM_DELETE_WINDOW", lambda: self._handle_decision(popup, "skip"))

        all_passed = t_failed == "0" and t_status == "Passato"
        badge_color = "#4CAF50" if all_passed else "#FF6B35"
        badge_text = (
            f"{t_passed} test passati - patch validata"
            if all_passed and has_patch
            else f"{t_passed} passati, {t_failed} falliti - revisione consigliata"
        )

        ctk.CTkLabel(
            popup,
            text=badge_text,
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=badge_color,
        ).pack(pady=(15, 5))

        tabs = ctk.CTkTabview(popup, width=900, height=560)
        tabs.pack(pady=5, padx=10)

        tab_diff = tabs.add("Diff patch")
        tab_test = tabs.add("Script test")
        tab_log_name = "Output test" if all_passed else "Output test - attenzione"
        tabs.add(tab_log_name)

        diff_frame = ctk.CTkFrame(tab_diff, fg_color="transparent")
        diff_frame.pack(fill="both", expand=True)

        txt_diff = tk.Text(
            diff_frame,
            font=("Courier New", 11),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            relief="flat",
            bd=0,
            wrap="none",
        )
        sb_y = tk.Scrollbar(diff_frame, orient="vertical", command=txt_diff.yview)
        sb_x = tk.Scrollbar(diff_frame, orient="horizontal", command=txt_diff.xview)
        txt_diff.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side="right", fill="y")
        sb_x.pack(side="bottom", fill="x")
        txt_diff.pack(fill="both", expand=True)

        txt_diff.tag_configure("added", background="#1e3a1e", foreground="#6fcf6f")
        txt_diff.tag_configure("removed", background="#3a1e1e", foreground="#f47676")
        txt_diff.tag_configure("header", foreground="#569cd6", font=("Courier New", 11, "bold"))
        txt_diff.tag_configure("hunk", foreground="#c586c0")
        txt_diff.tag_configure("neutral", foreground="#9a9a9a")
        txt_diff.tag_configure("notice", foreground="#FFA726", font=("Courier New", 11, "italic"))

        if diff_lines:
            for line in diff_lines:
                if line.startswith("+++") or line.startswith("---"):
                    txt_diff.insert("end", line, "header")
                elif line.startswith("@@"):
                    txt_diff.insert("end", line, "hunk")
                elif line.startswith("+"):
                    txt_diff.insert("end", line, "added")
                elif line.startswith("-"):
                    txt_diff.insert("end", line, "removed")
                else:
                    txt_diff.insert("end", line, "neutral")
        else:
            if t_status == "Fallito":
                msg = (
                    "La patch non e disponibile perche non ha superato la validazione locale.\n\n"
                    "Puoi leggere il test e l'output per capire se serve un controllo manuale."
                )
            else:
                msg = (
                    "L'AI non ha proposto modifiche al file target.\n\n"
                    "Puoi leggere il test e l'output per capire se serve un controllo manuale."
                )
            txt_diff.insert("end", msg, "notice")

        txt_diff.configure(state="disabled")

        txt_test = ctk.CTkTextbox(tab_test, width=880, height=500, font=("Courier New", 11))
        txt_test.pack(fill="both", expand=True)
        txt_test.insert("0.0", test_code or "Script di test non generato.")

        txt_log = ctk.CTkTextbox(
            tabs.tab(tab_log_name),
            width=880,
            height=500,
            font=("Courier New", 11),
        )
        txt_log.pack(fill="both", expand=True)
        txt_log.insert("0.0", t_log or "Nessun output disponibile.")

        if not all_passed:
            tabs.set(tab_log_name)

        btn_frame = ctk.CTkFrame(popup, fg_color="transparent")
        btn_frame.pack(pady=15)

        apply_label = "Applica patch" if has_patch else "Nessuna patch da applicare"
        apply_color = "#2e7d32" if all_passed and has_patch else "#8a6d1d"

        ctk.CTkButton(
            btn_frame,
            text=apply_label,
            fg_color=apply_color,
            hover_color="#1b5e20" if all_passed and has_patch else "#6f5614",
            width=220,
            command=lambda: self._handle_decision(popup, "queue"),
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame,
            text="Scarta",
            fg_color="#795500",
            hover_color="#5c3d00",
            width=120,
            command=lambda: self._handle_decision(popup, "skip"),
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame,
            text="Forza push",
            fg_color="#b71c1c",
            hover_color="#7f0000",
            width=140,
            command=lambda: self._handle_decision(popup, "force"),
        ).pack(side="right", padx=10)

    def _handle_decision(self, popup: ctk.CTkToplevel, decision: str) -> None:
        if decision == "queue":
            self._apply_current_patch()
        elif decision == "skip":
            with self._lock:
                self.action_taken = "Scartato"
            if self.target_file:
                self.safe_log(f"[-] Modifiche scartate per: {self.target_file.name}")
        elif decision == "force":
            self.force_push_requested = True
            with self._lock:
                self.action_taken = "Forza push"
            self.safe_log("[!] Forza push richiesto. Le analisi successive saranno saltate.")

        try:
            popup.destroy()
        except tk.TclError:
            pass

        self._decision_event.set()

    def _apply_current_patch(self) -> None:
        if not self.target_file:
            return

        if not self._current_has_patch:
            with self._lock:
                self.action_taken = "Nessuna patch applicabile"
            self.safe_log("[i] Nessuna patch applicabile: il file rimane invariato.")
            return

        abs_path = self.target_file.resolve()
        try:
            backup_dir = self.git_dir / "ai_agent_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{int(time.time())}_{abs_path.name}.bak"
            shutil.copy2(abs_path, backup_path)

            abs_path.write_text(self.fixed_code, encoding="utf-8")
            self.patched_files_list.append(abs_path)
            self.backup_files.append(backup_path)

            with self._lock:
                self.action_taken = "Patch applicata"
                self._patched_count += 1

            self.safe_log(f"[+] Patch salvata su disco per: {abs_path.name}")
        except Exception as exc:
            with self._lock:
                self.action_taken = "Errore scrittura patch"
            self.safe_log(f"[!] Impossibile scrivere la patch: {exc}")

