import os
import sys
import subprocess
import re
import threading
import difflib
import stat
import csv
import time
from datetime import datetime
import tkinter as tk
from tkinter import filedialog
from google import genai
import customtkinter as ctk

# ==========================================
# MODULO GIT E I/O 
# ==========================================
class GitManager:
    @staticmethod
    def get_modified_files():
        """Estrae l'elenco dei file modificati nel commit corrente."""
        try:
            cmd = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            files = res.stdout.strip().split('\n')
            return [os.path.normpath(f) for f in files if f.strip()]
        except Exception:
            return []

    @staticmethod
    def get_context_files(target_file, max_files=3):
        """Implementa la Context Awareness estraendo file limitrofi (Focal Method)."""
        target_dir = os.path.dirname(os.path.abspath(target_file))
        context_files = []
        target_ext = os.path.splitext(target_file)[1]
        
        if not os.path.exists(target_dir): return []

        for f in os.listdir(target_dir):
            full_path = os.path.join(target_dir, f)
            if full_path != os.path.abspath(target_file) and f.endswith(target_ext):
                context_files.append(full_path)
                if len(context_files) >= max_files: break
        return context_files

    @staticmethod
    def read_files(file_list):
        content = ""
        for file_name in file_list:
            if not os.path.exists(file_name) or os.path.isdir(file_name):
                continue
            try:
                with open(file_name, "r", encoding="utf-8") as f:
                    content += f"\n\n--- FILE: {os.path.basename(file_name)} ---\n{f.read()}\n"
            except Exception:
                continue
        return content

import time # Assicurati che 'time' sia importato in cima al file

# ==========================================
# MODULO GEN-AI CLOUD
# ==========================================
class GenAIClient:
    def __init__(self, api_key):
        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-1.5-flash" # Prova anche "gemini-1.5-pro" se flash è intasato

    def analyze_code(self, target_file, source_code, context_code=""):
        prompt = (
            "Sei un Code Reviewer automatizzato. Analizza questo codice e i file di contesto:\n\n"
            f"FILE TARGET: {target_file}\nCODICE TARGET:\n{source_code}\n\n"
            f"CONTESTO ARCHITETTURALE:\n{context_code}\n\n"
            "REGOLE:\n"
            "1. Trova falle logiche reali nel FILE TARGET. Ignora stile o formattazione.\n"
            "2. Se trovi un bug, fornisci: ## ANALISI DELL'ERRORE, ## CODICE CORRETTO e ## UNIT TEST.\n"
            "3. Se NON ci sono bug, scrivi 'Nessun bug' e fornisci un UNIT TEST basilare che passi.\n"
            "4. Termina tassativamente con:\n"
            "   DEPENDENCIES: [pacchetti o NONE]\n"
            "   TEST_FILE_NAME: [nome file]\n"
            "   RUN_COMMAND: [comando di test es. pytest test.py]\n"
        )
        
        # --- MECCANISMO DI RETRY (Tolleranza ai Guasti API) ---
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return self.client.models.generate_content(model=self.model_name, contents=prompt).text
            except Exception as e:
                # Se l'errore è un 503 (Server Pieni), aspetta e riprova
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 5  # Aspetta 5 secondi, poi 10...
                        print(f"API di Google sature. Tentativo {attempt + 2} di {max_retries} tra {wait_time} secondi...")
                        time.sleep(wait_time)
                    else:
                        raise Exception(f"I server di Google sono in down dopo {max_retries} tentativi. Errore: {e}")
                else:
                    raise e # Se è un errore di chiave API o altro, blocca subito
# ==========================================
# MODULO TELEMETRIA (Salvataggio Metriche)
# ==========================================
class ExperimentLogger:
    LOG_FILE = "thesis_metrics.csv"

    @staticmethod
    def initialize():
        """Inizializza il CSV se non esiste, utile per raccogliere i dati per la tesi."""
        if not os.path.exists(ExperimentLogger.LOG_FILE):
            try:
                with open(ExperimentLogger.LOG_FILE, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        "Timestamp", 
                        "File Analizzato", 
                        "Esito LLM", 
                        "Stato Test Locale", 
                        "Azione Utente", 
                        "Tempo AI (sec)"
                    ])
            except Exception as e:
                print(f"Errore creazione logger: {e}")

    @staticmethod
    def log_run(target_file, llm_status, test_status, human_action, response_time):
        """Aggiunge una riga al file CSV con i risultati del test corrente."""
        try:
            with open(ExperimentLogger.LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    os.path.basename(target_file) if target_file else "N/A",
                    llm_status,
                    test_status,
                    human_action,
                    round(response_time, 2)
                ])
        except Exception as e:
            print(f"Errore nel salvataggio della telemetria: {e}")

# ==========================================
# CLASSE PRINCIPALE GUI (Human-in-the-Loop)
# ==========================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class GitAgentApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Git Pre-Push AI Reviewer")
        self.geometry("850x600")
        self.protocol("WM_DELETE_WINDOW", self.bypass_hook)

        self.fixed_code = ""
        self.target_file = ""
        
        # Variabili di stato per la telemetria
        ExperimentLogger.initialize()
        self.start_time = time.time()
        self.llm_status = "In attesa"
        self.test_status = "N/A"

        self.lbl_title = ctk.CTkLabel(self, text="Code Review", font=ctk.CTkFont(size=22, weight="bold"))
        self.lbl_title.pack(pady=(20, 10))

        self.log_box = ctk.CTkTextbox(self, width=800, height=400)
        self.log_box.pack(pady=10)

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(pady=20)

        self.btn_approve = ctk.CTkButton(self.btn_frame, text="Approva e Pusha", fg_color="green", hover_color="darkgreen", command=self.approve_push, state="disabled")
        self.btn_approve.grid(row=0, column=0, padx=10)

        self.btn_block = ctk.CTkButton(self.btn_frame, text="Blocca Push", fg_color="red", hover_color="darkred", command=self.block_push)
        self.btn_block.grid(row=0, column=1, padx=10)

        self.btn_fix = ctk.CTkButton(self.btn_frame, text="Visualizza Diff e Applica", fg_color="#b8860b", text_color="white", hover_color="#8b6508", command=self.show_diff_viewer, state="disabled")
        self.btn_fix.grid(row=0, column=2, padx=10)

        self.safe_log("Avvio analisi automatica del commit...")
        
        # Esecuzione asincrona per non bloccare la GUI
        threading.Thread(target=self.run_agent_logic, daemon=True).start()

    def safe_log(self, text):
        """Metodo thread-safe per aggiornare la GUI dal thread asincrono"""
        self.after(0, lambda: self.log_box.insert("end", text + "\n"))
        self.after(0, lambda: self.log_box.see("end"))

    def safe_btn_state(self, btn, state):
        self.after(0, lambda: btn.configure(state=state))

    def _log_and_exit(self, exit_code, human_action):
        """Helper per salvare le metriche prima di chiudere"""
        elapsed = time.time() - self.start_time
        ExperimentLogger.log_run(self.target_file, self.llm_status, self.test_status, human_action, elapsed)
        self.destroy()
        os._exit(exit_code)

    def approve_push(self):
        self._log_and_exit(0, "Push Approvato")

    def block_push(self):
        self._log_and_exit(1, "Push Bloccato")

    def bypass_hook(self):
        self._log_and_exit(0, "Bypass (Finestra chiusa)")

    def show_diff_viewer(self):
        if not self.fixed_code or not self.target_file: return

        try:
            with open(self.target_file, "r", encoding="utf-8") as f:
                old_code = f.readlines()
        except Exception:
            old_code = []

        new_code = self.fixed_code.splitlines(keepends=True)
        diff_output = "".join(difflib.unified_diff(old_code, new_code, fromfile='Originale', tofile='Patch AI'))

        popup = ctk.CTkToplevel(self)
        popup.title("Diff Viewer")
        popup.geometry("750x500")
        popup.grab_set() 
        
        txt = ctk.CTkTextbox(popup, width=700, height=350, font=("Courier", 12))
        txt.pack(pady=20)
        txt.insert("0.0", diff_output if diff_output else "Nessuna differenza strutturale.")
        
        def apply_changes():
            with open(self.target_file, "w", encoding="utf-8") as f:
                f.write(self.fixed_code)
            self.safe_log(f"File {self.target_file} patchato con successo. Esegui un nuovo commit.")
            self.safe_btn_state(self.btn_approve, "disabled")
            self.safe_btn_state(self.btn_fix, "disabled")
            
            # Registra l'azione specifica di applicazione patch
            elapsed = time.time() - self.start_time
            ExperimentLogger.log_run(self.target_file, self.llm_status, self.test_status, "Patch Applicata", elapsed)
            
            popup.destroy()

        ctk.CTkButton(popup, text="Applica Patch", fg_color="green", command=apply_changes).pack(side="left", padx=50, pady=10)
        ctk.CTkButton(popup, text="Annulla", fg_color="gray", command=popup.destroy).pack(side="right", padx=50, pady=10)

    def run_agent_logic(self):
        try:
            api_key = os.getenv('GOOGLE_API_KEY')
            if not api_key:
                self.llm_status = "Errore API Key"
                self.safe_log("Errore: GOOGLE_API_KEY non configurata nelle variabili d'ambiente.")
                self.safe_btn_state(self.btn_approve, "normal") 
                return

            modified_files = GitManager.get_modified_files()
            valid_extensions = ('.py', '.dart', '.swift', '.js', '.ts', '.java', '.go', '.cpp', '.c', '.cs')
            target_files = [f for f in modified_files if f.endswith(valid_extensions)]

            if not target_files:
                self.llm_status = "Nessun file supportato"
                self.safe_log("Nessun file sorgente modificato. Push consentito.")
                self.safe_btn_state(self.btn_approve, "normal")
                return

            self.target_file = target_files[0]
            source_code = GitManager.read_files([self.target_file])
            
            context_files = GitManager.get_context_files(self.target_file)
            context_code = GitManager.read_files(context_files) if context_files else "Nessun contesto aggiuntivo."

            self.safe_log(f"Analisi di {self.target_file} in corso...\nRecupero contesto: {len(context_files)} file limitrofi.")
            
            ai_client = GenAIClient(api_key)
            response_text = ai_client.analyze_code(self.target_file, source_code, context_code)

            with open("REVIEW_REPORT.md", "w", encoding="utf-8") as report:
                report.write(response_text)

            match_code = re.search(r"## CODICE CORRETTO.*?```[^\n]*\n(.*?)\n```", response_text, re.DOTALL)
            if match_code: 
                self.fixed_code = match_code.group(1).strip()

            cmd_match = re.search(r"RUN_COMMAND:\s*(.*)", response_text)
            t_file_match = re.search(r"TEST_FILE_NAME:\s*(\S+)", response_text)

            if "Nessun bug" in response_text or "nessun bug" in response_text.lower():
                self.llm_status = "Nessun Bug Rilevato"
                self.safe_log("L'AI non ha rilevato falle logiche. Push consentito.")
                self.safe_btn_state(self.btn_approve, "normal")
            elif cmd_match and t_file_match:
                self.llm_status = "Bug Rilevato"
                cmd = cmd_match.group(1).strip()
                t_file = t_file_match.group(1).strip()
                
                blocks = re.findall(r"```[^\n]*\n(.*?)\n```", response_text, re.DOTALL)
                if blocks:
                    with open(t_file, "w", encoding="utf-8") as f: 
                        f.write(blocks[-1])
                    
                    self.safe_log(f"Falla logica rilevata. Avvio validazione deterministica: {cmd}")
                    exec_cmd = f"{sys.executable} -m {cmd}" if cmd.startswith("pytest") else cmd
                    res = subprocess.run(exec_cmd, shell=True, capture_output=True, text=True)

                    if res.returncode == 0:
                        self.test_status = "Passato"
                        self.safe_log("La patch proposta ha superato gli Unit Test in locale.")
                        self.safe_btn_state(self.btn_fix, "normal")
                    else:
                        self.test_status = "Fallito (Possibile Overfitting)"
                        self.safe_log(f"Allarme: La patch generata fallisce i test!\nLog: {res.stderr[:200]}")
                        self.safe_btn_state(self.btn_fix, "normal")

        except Exception as e: 
            self.llm_status = "Errore di Sistema"
            self.safe_log(f"Eccezione di sistema: {e}")
            self.safe_btn_state(self.btn_approve, "normal")

# ==========================================
# INSTALLAZIONE AUTOMATICA HOOK
# ==========================================
def install_hook():
    root = tk.Tk()
    root.withdraw()
    print("Seleziona la root del progetto Git...")
    target_dir = filedialog.askdirectory(title="Seleziona il repository Git")
    
    if not target_dir: sys.exit(1)
    hooks_dir = os.path.join(target_dir, ".git", "hooks")
    if not os.path.exists(hooks_dir):
        print("Errore: cartella .git/hooks non trovata.")
        sys.exit(1)
        
    pre_push_path = os.path.join(hooks_dir, "pre-push")
    script_path = os.path.abspath(__file__).replace("\\", "/") 
    python_exe = sys.executable.replace("\\", "/")

    bash_hook = f"#!/bin/sh\n\"{python_exe}\" \"{script_path}\"\nexit $?\n"
    try:
        with open(pre_push_path, "w", encoding="utf-8") as f: f.write(bash_hook)
        os.chmod(pre_push_path, os.stat(pre_push_path).st_mode | stat.S_IEXEC)
        print(f"Hook installato con successo in: {pre_push_path}")
    except Exception as e:
        print(f"Errore: {e}")
    sys.exit(0)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--install":
        install_hook()
    else:
        app = GitAgentApp()
        app.mainloop()