import os
import sys
import subprocess
import re
import threading
from google import genai
import customtkinter as ctk

# --- 1. FUNZIONI HELPER ---
def leggi_contenuto_file(lista_nomi):
    testo_accumulato = ""
    for nome in lista_nomi:
        if not os.path.exists(nome) or os.path.isdir(nome):
            continue
        try:
            with open(nome, "r", encoding="utf-8") as f:
                testo_accumulato += f"\n\n--- INIZIO FILE: {nome} ---\n"
                testo_accumulato += f.read()
                testo_accumulato += f"\n--- FINE FILE: {nome} ---\n"
        except Exception:
            continue
    return testo_accumulato

def rileva_nomi_modificati():
    try:
        comando = ['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD']
        risultato = subprocess.run(comando, capture_output=True, text=True, check=True)
        nomi = risultato.stdout.strip().split('\n')
        return [os.path.normpath(n) for n in nomi if n.strip()]
    except Exception as e:
        return []

# --- 2. INTERFACCIA E LOGICA ---
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class AgenteIAApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("🛡️ AI Git Pre-Push Reviewer")
        self.geometry("850x600")

        # Variabili nascoste per ricordare la correzione
        self.codice_riparato = ""
        self.file_da_riparare = ""

        # UI Elements
        self.label_titolo = ctk.CTkLabel(self, text="Revisione Codice Pre-Push", font=ctk.CTkFont(size=22, weight="bold"))
        self.label_titolo.pack(pady=(20, 10))

        self.textbox_log = ctk.CTkTextbox(self, width=800, height=400)
        self.textbox_log.pack(pady=10)

        # Frame per i bottoni finali
        self.frame_bottoni = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_bottoni.pack(pady=20)

        self.btn_approva = ctk.CTkButton(self.frame_bottoni, text="✅ Approva e Pusha", fg_color="green", hover_color="darkgreen", command=self.approva_push, state="disabled")
        self.btn_approva.grid(row=0, column=0, padx=10)

        self.btn_blocca = ctk.CTkButton(self.frame_bottoni, text="❌ Blocca Push", fg_color="red", hover_color="darkred", command=self.blocca_push)
        self.btn_blocca.grid(row=0, column=1, padx=10)

        self.btn_approva_codice = ctk.CTkButton(self.frame_bottoni, text="✨ Applica Modifiche IA", fg_color="#b8860b", text_color="white", hover_color="#8b6508", command=self.modifica_codice_ia, state="disabled")
        self.btn_approva_codice.grid(row=0, column=2, padx=10)

        # Avvio automatico immediato all'apertura
        self.scrivi_log("Hook intercettato! Avvio analisi automatica in corso...")
        threading.Thread(target=self.esegui_logica_agente).start()

    def scrivi_log(self, testo):
        self.textbox_log.insert("end", testo + "\n")
        self.textbox_log.see("end")

    # --- COMANDI PER I BOTTONI ---
    def approva_push(self):
        self.scrivi_log("Push approvato. Chiusura app...")
        self.destroy()
        os._exit(0) # 0 = Via libera a Git

    def blocca_push(self):
        self.scrivi_log("Push bloccato. Chiusura app...")
        self.destroy()
        os._exit(1) # 1 = Ferma Git

    def modifica_codice_ia(self):
        if not self.codice_riparato or not self.file_da_riparare:
            self.scrivi_log("❌ Impossibile riparare: Dati non trovati in memoria.")
            return

        try:
            # Sovrascrive il file originale con la versione corretta dell'IA
            with open(self.file_da_riparare, "w", encoding="utf-8") as f:
                f.write(self.codice_riparato)
            
            self.scrivi_log(f"\n✨ MAGIA APPLICATA! Il file {self.file_da_riparare} è stato sovrascritto.")
            self.scrivi_log("⚠️ ATTENZIONE: Questo push DEVE essere bloccato perché Git stava spedendo il codice vecchio.")
            self.scrivi_log("👉 Clicca su '❌ Blocca Push'. Poi nel terminale fai: 'git add .', poi 'git commit -m \"Fix IA\"', e infine riprova a pushare.")
            
            # Blocca gli altri bottoni per evitare disastri
            self.btn_approva_codice.configure(state="disabled")
            self.btn_approva.configure(state="disabled")
            
        except Exception as e:
            self.scrivi_log(f"❌ Errore durante il salvataggio: {e}")

    # --- LOGICA DELL'AGENTE ---
    def esegui_logica_agente(self):
        try:
            KEY = os.getenv('GOOGLE_API_KEY')
            if not KEY:
                self.scrivi_log("❌ ERRORE: Chiave API mancante.")
                return

            self.scrivi_log("Cerco i file modificati da pushare...")
            nomi_cambiati = rileva_nomi_modificati()

            if not nomi_cambiati:
                self.scrivi_log("Nessun file rilevato. Puoi approvare il push.")
                self.btn_approva.configure(state="normal")
                return

            self.scrivi_log(f"File rilevati: {nomi_cambiati}")
            codice_target = leggi_contenuto_file(nomi_cambiati)

            client = genai.Client(api_key=KEY)
            prompt = (
                "Sei un Senior Software Engineer. Stai analizzando un codice prima del push.\n\n"
                f"CODICE:\n{codice_target}\n\n"
                "Istruzioni TASSATIVE:\n"
                "1. Scrivi ## ANALISI DELL'ERRORE (max 3 righe).\n"
                "2. Scrivi ## CODICE CORRETTO in markdown.\n"
                "3. Scrivi ## UNIT TEST in markdown.\n"
                "4. Fornisci queste TRE righe esatte alla fine del testo:\n"
                "   DEPENDENCIES: [nomi dei pacchetti da installare via pip, es. pytest requests. Scrivi NONE se non serve nulla]\n"
                "   TEST_FILE_NAME: [nome file test]\n"
                "   RUN_COMMAND: [comando test]\n"
            )
            
            self.scrivi_log("Inoltro codice a Gemini...")
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            self.scrivi_log("✅ Risposta ricevuta!")

            with open("REPORT_AGENTE_IA.md", "w", encoding="utf-8") as report:
                report.write(response.text)

            # --- ESTRAZIONE DELLA CORREZIONE AUTOMATICA ---
            match_correzione = re.search(r"## CODICE CORRETTO.*?```[^\n]*\n(.*?)\n```", response.text, re.DOTALL)
            
            # Se trova il codice corretto, lo tiene in memoria e lo stampa a schermo
            if match_correzione and len(nomi_cambiati) == 1:
                self.codice_riparato = match_correzione.group(1).strip()
                self.file_da_riparare = nomi_cambiati[0]
                self.scrivi_log("\n" + "="*50)
                self.scrivi_log("💡 L'IA HA PREPARATO UNA SOLUZIONE:\n")
                self.scrivi_log(self.codice_riparato)
                self.scrivi_log("="*50 + "\n")

            # --- ESTRAZIONE DATI TEST ---
            deps_match = re.search(r"DEPENDENCIES:\s*(.*)", response.text)
            test_file_match = re.search(r"TEST_FILE_NAME:\s*(\S+)", response.text)
            run_command_match = re.search(r"RUN_COMMAND:\s*(.*)", response.text)

            if deps_match and test_file_match and run_command_match:
                dipendenze = deps_match.group(1).strip()
                test_file_name = test_file_match.group(1).strip()
                run_command = run_command_match.group(1).strip()

                if dipendenze.upper() != "NONE" and dipendenze != "":
                    self.scrivi_log(f"📦 Installazione dipendenze: {dipendenze}...")
                    subprocess.run(f"pip install {dipendenze}", shell=True, capture_output=True)
                    self.scrivi_log("✅ Dipendenze installate.")

                blocchi_codice = re.findall(r"```[^\n]*\n(.*?)\n```", response.text, re.DOTALL)
                if blocchi_codice:
                    codice_test = blocchi_codice[-1]
                    with open(test_file_name, "w", encoding="utf-8") as f:
                        f.write(codice_test)
                    
                    self.scrivi_log(f"🧪 Esecuzione test: {run_command} ...")
                    risultato = subprocess.run(run_command, shell=True, capture_output=True, text=True)
                    
                    # LA LOGICA CORRETTA DEL BOTTONE GIALLO E' QUI SOTTO:
                    if risultato.returncode == 0:
                        self.scrivi_log("✅ TEST PASSATO! Il codice originale non aveva bug. È sicuro da pushare.")
                        self.btn_approva.configure(state="normal")
                    else:
                        self.scrivi_log("❌ TEST FALLITO! È stato rilevato un bug nel tuo codice attuale.")
                        self.scrivi_log(f"Dettaglio:\n{risultato.stdout.strip()}")
                        
                        # Il test è fallito. Se l'IA ha scritto una correzione, accendiamo il bottone giallo!
                        if self.codice_riparato:
                            self.scrivi_log("\n👉 Premi il bottone 'Applica Modifiche IA' per correggere il file.")
                            self.btn_approva_codice.configure(state="normal")

        except Exception as e:
            self.scrivi_log(f"❌ Errore critico: {e}")
        finally:
            self.scrivi_log("\n👉 Operazione terminata. Usa i bottoni in basso per decidere.")

if __name__ == "__main__":
    app = AgenteIAApp()
    app.mainloop()