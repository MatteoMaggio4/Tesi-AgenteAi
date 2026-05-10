import time
from pathlib import Path

from google import genai


class GenAIClient:
    MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def analyze_code(
        self,
        target_file: Path,
        source_code: str,
        context_code: str = "",
        error_feedback: str = "",
    ) -> str:
        prompt = self._build_prompt(target_file, source_code, context_code, error_feedback)

        for model_name in self.MODELS:
            print(f"[API] Connessione al modello: {model_name}")
            for attempt in range(2):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                    return response.text or ""
                except Exception as exc:
                    err = str(exc)
                    if "404" in err:
                        print(f"[API] Modello non disponibile: {model_name}.")
                        break
                    if any(marker in err for marker in ("503", "UNAVAILABLE", "429")):
                        if attempt == 0:
                            print("[API] Servizio temporaneamente saturo. Riprovo tra 5 secondi.")
                            time.sleep(5)
                            continue
                        print(f"[API] Modello non raggiungibile: {model_name}.")
                        break

                    print(f"[API] Errore su {model_name}: {exc}")
                    break

        raise RuntimeError(
            "Nessun modello Gemini disponibile. Controllare connessione e API key."
        )

    @staticmethod
    def _build_prompt(
        target_file: Path,
        source_code: str,
        context_code: str,
        error_feedback: str,
    ) -> str:
        prompt = (
            "Sei un revisore automatico di codice per un progetto universitario.\n"
            "Analizza solo difetti logici, bug reali e casi limite rilevanti. "
            "Ignora stile, formattazione e preferenze personali.\n\n"
            f"FILE TARGET: {target_file}\n"
            f"CODICE TARGET:\n{source_code}\n\n"
            f"CONTESTO ARCHITETTURALE:\n{context_code}\n\n"
            "Formato obbligatorio della risposta:\n"
            "- Se trovi un bug, usa queste sezioni:\n"
            "  ## ANALISI DELL'ERRORE\n"
            "  ## CODICE CORRETTO\n"
            "  ```linguaggio\n"
            "  <file target completo corretto>\n"
            "  ```\n"
            "  ## UNIT TEST\n"
            "  ```linguaggio\n"
            "  <test flat e autonomo>\n"
            "  ```\n"
            "- Se non trovi bug, scrivi chiaramente 'Nessun bug' e fornisci comunque "
            "un test basilare di convalida.\n\n"
            "Vincoli sui test:\n"
            "1. Niente framework esterni: no pytest, junit, database o servizi remoti.\n"
            "2. Il test deve stampare una riga leggibile per ogni caso verificato:\n"
            "   [PASS] <descrizione del caso>\n"
            "   [FAIL] <descrizione del caso>: <motivo>\n"
            "3. L'output deve includere sia i casi superati sia quelli falliti, non solo gli errori.\n"
            "4. Alla fine il test deve stampare esattamente queste metriche:\n"
            "   Passed: <numero>\n"
            "   Failed: <numero>\n"
            "5. Il test deve terminare con exit code 0 se passa e non-zero se fallisce.\n"
            "6. Il test deve essere uno script Python flat: niente unittest, pytest, classi di test o decorator.\n"
            "7. Per verificare eccezioni usa try/except espliciti oppure lambda/funzioni senza argomenti.\n"
            "8. Non mescolare mai argomenti posizionali dopo keyword argument: e SyntaxError in Python.\n"
            "9. Non inventare requisiti non presenti nel codice o nella docstring del file target.\n"
            "10. Non proporre comandi distruttivi o comandi che non eseguono il test.\n\n"
            "Concludi sempre fuori dai blocchi di codice con:\n"
            "DEPENDENCIES: NONE\n"
            "TEST_FILE_NAME: <nome_file_test>\n"
            "RUN_COMMAND: <comando_per_eseguire_il_test>\n"
        )

        if error_feedback:
            prompt += (
                "\n\n[FEEDBACK ESECUZIONE PRECEDENTE]\n"
                "Il test precedente non e stato eseguibile o non ha rispettato il formato.\n"
                "Correggi risposta, test o patch mantenendo i vincoli sopra.\n"
                f"Output ricevuto:\n```\n{error_feedback}\n```\n"
            )

        return prompt
