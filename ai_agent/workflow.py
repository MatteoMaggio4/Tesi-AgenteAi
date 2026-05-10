import time
from pathlib import Path
from typing import Optional

from .bypass import set_bypass_flag
from .config import MAX_FILES_TO_ANALYZE, MAX_RETRY_ATTEMPTS
from .credentials import resolve_api_key
from .experiment_logger import ExperimentLogger
from .genai_client import GenAIClient
from .git_manager import GitManager
from .git_utils import to_git_path
from .process_utils import run_process


class AgentWorkflowMixin:
    def run_agent_logic(self) -> None:
        try:
            api_key = resolve_api_key(self.repo_root)
            if not api_key:
                self.safe_log("[!] API key non trovata.")
                self.safe_log("    Usa GOOGLE_API_KEY oppure un file .api_key nella root del repository.")
                self.safe_log("    Il push viene autorizzato senza analisi.")
                time.sleep(2)
                return self._request_exit(0)

            target_files = GitManager.get_files_changed_by_push(self.pre_push_stdin)

            if len(target_files) > MAX_FILES_TO_ANALYZE:
                self.safe_log(
                    f"[!] Rilevati {len(target_files)} file. "
                    f"Analizzo solo i primi {MAX_FILES_TO_ANALYZE}."
                )
                target_files = target_files[:MAX_FILES_TO_ANALYZE]

            if not target_files:
                self.safe_log("Nessun file sorgente rilevante nel push. Push autorizzato.")
                time.sleep(1)
                return self._request_exit(0)

            self._total_files = len(target_files)
            self._set_status(f"[analisi: {self._total_files} file]", "#FFA726")
            ai_client = GenAIClient(api_key)

            for idx, file_path in enumerate(target_files, start=1):
                if self.force_push_requested or self._should_stop():
                    break

                self.target_file = file_path
                api_time = 0.0
                file_start = time.time()

                self.safe_log("\n" + "=" * 60)
                self.safe_log(f"[{idx}/{self._total_files}] Analisi file: {file_path.name}")

                size_check = GitManager.is_file_too_large(file_path)
                if size_check.too_large:
                    self.safe_log(f"  [!] File saltato: {size_check.reason}.")
                    ExperimentLogger.log_run(
                        self.repo_root,
                        file_path,
                        "Saltato per dimensione",
                        "N/A",
                        "0",
                        "0",
                        "Saltato automaticamente",
                        0.0,
                        round(time.time() - file_start, 2),
                    )
                    continue

                self._reset_file_state()
                error_feedback = ""
                bug_or_risk_found = False
                llm_status = "Non conclusivo"

                for attempt in range(MAX_RETRY_ATTEMPTS):
                    if self._should_stop():
                        break

                    self.safe_log(
                        f"  [Iterazione {attempt + 1}/{MAX_RETRY_ATTEMPTS}] "
                        "Richiesta analisi AI..."
                    )

                    t0 = time.time()
                    response_text = self._fetch_ai_response(ai_client, file_path, error_feedback)
                    api_time += time.time() - t0

                    if not response_text:
                        self.safe_log("  [!] Risposta AI vuota.")
                        llm_status = "Risposta vuota"
                        break

                    result, err_log = self._handle_ai_response(response_text)

                    if result == "bug":
                        bug_or_risk_found = True
                        llm_status = "Bug o rischio segnalato"
                        self.safe_log(
                            f"  [test] Passed: {self.tests_passed} | Failed: {self.tests_failed}"
                        )
                        break

                    if result == "clean":
                        llm_status = "Nessun bug"
                        self.safe_log("  [ok] Nessuna criticita logica segnalata.")
                        break

                    if result == "failed":
                        llm_status = "Test non eseguibile"
                        error_feedback = err_log or "Esecuzione fallita senza output utile."
                        self.safe_log("  [!] Test non eseguibile. Invio feedback all'AI.")
                        continue

                    llm_status = "Formato non valido"
                    error_feedback = err_log or (
                        "La risposta AI non rispetta il formato richiesto: "
                        "mancano metadati o blocchi di test validi."
                    )
                    self.safe_log("  [!] Risposta AI incompleta. Invio feedback all'AI.")
                    continue

                self._analyzed_files += 1

                if self._should_stop():
                    break

                needs_review = bug_or_risk_found or self.test_status == "Fallito"
                if needs_review:
                    self.safe_log("  [review] Richiesta decisione manuale.")
                    self._decision_event.clear()
                    self.after(0, self.show_diff_viewer)
                    self._decision_event.wait()
                    if self._should_stop():
                        break
                else:
                    self.safe_log("  [ok] File concluso senza interventi.")

                session_time = round(time.time() - file_start, 2)
                with self._lock:
                    ExperimentLogger.log_run(
                        self.repo_root,
                        file_path,
                        llm_status,
                        self.test_status,
                        self.tests_passed,
                        self.tests_failed,
                        self.action_taken,
                        round(api_time, 2),
                        session_time,
                    )

            self.safe_log("\n" + "=" * 60)
            self._finalize()

        except Exception as exc:
            self.safe_log(f"\n[Eccezione interna] {exc}")
            time.sleep(2)
            self._request_exit(0)

    def _reset_file_state(self) -> None:
        with self._lock:
            self.tests_passed = "0"
            self.tests_failed = "0"
            self.test_status = "N/A"
            self.test_output_log = ""
            self.action_taken = "Nessuna azione"
            self.fixed_code = ""
            self.generated_test_code = ""
            self._current_has_patch = False

    def _finalize(self) -> None:
        if self.force_push_requested:
            self.safe_log("Forza push attivo. Il push originale viene autorizzato.")
            self._set_status("[push forzato]", "#FF6B35")
            time.sleep(2)
            return self._request_exit(0)

        changed_files = [
            path for path in self.patched_files_list
            if path.exists() and GitManager.has_worktree_changes(path, self.repo_root)
        ]

        if changed_files:
            self.safe_log(
                f"Riepilogo: {self._total_files} file considerati, "
                f"{self._analyzed_files} analizzati, {self._patched_count} patch applicate."
            )
            self.safe_log(f"Creo un commit separato per {len(changed_files)} file patchati.")

            rel_paths = [to_git_path(path, self.repo_root) for path in changed_files]
            commit_msg = f"Auto-patch AI: correzioni su {len(changed_files)} file"

            commit_res = run_process(
                ["git", "commit", "--only", "-m", commit_msg, "--"] + rel_paths,
                cwd=self.repo_root,
            )

            if commit_res.returncode != 0:
                self.safe_log("[!] Commit automatico non riuscito.")
                self.safe_log(commit_res.stderr.strip() or commit_res.stdout.strip())
                self.safe_log("    Il push viene bloccato per permettere un controllo manuale.")
                time.sleep(4)
                return self._request_exit(1)

            set_bypass_flag()
            self._cleanup_backups()
            self._set_status("[validazione terminata]", "#4CAF50")
            self.safe_log("Commit automatico creato correttamente.")
            self.safe_log("Riesegui git push: il bypass temporaneo evita un doppio controllo immediato.")
            time.sleep(4)
            return self._request_exit(1)

        self._cleanup_backups()
        self.safe_log(
            f"Riepilogo: {self._total_files} file considerati, "
            f"{self._analyzed_files} analizzati, nessuna patch salvata."
        )
        self._set_status("[validazione ok]", "#4CAF50")
        self.safe_log("Validazione conclusa. Push autorizzato.")
        time.sleep(2)
        return self._request_exit(0)

    # ------------------------------------------------------------------
    # Parsing risposta AI e test
    # ------------------------------------------------------------------

    def _fetch_ai_response(
        self,
        ai_client: GenAIClient,
        target_file: Path,
        error_feedback: str = "",
    ) -> Optional[str]:
        source_code = GitManager.read_files([target_file], self.repo_root)
        context_code = GitManager.read_files(
            GitManager.get_context_files(target_file),
            self.repo_root,
        )
        try:
            return ai_client.analyze_code(target_file, source_code, context_code, error_feedback)
        except Exception as exc:
            self.safe_log(f"  [!] Errore API: {exc}")
            return None
