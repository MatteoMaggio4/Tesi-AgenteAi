import subprocess
from google import genai
import os

CHIAVE_API=os.environ.get("GOOGLE_API_KEY")
if not CHIAVE_API:
   print("Attenzione: la chiave API non è stata trovata. Assicurati di averla impostata come variabile d'ambiente 'GOOGLE_API_KEY'.") 
   exit(1)
client = genai.Client(api_key=CHIAVE_API)
numero_tentativi=1
errore_precedente = ""
print("Creazione della memoria dell'IA (Chat)...")
chat = client.chats.create(model='gemini-2.5-flash')
while numero_tentativi<=3:
    print(f"\nTentativo numero {numero_tentativi}...")
    if numero_tentativi== 1:
     prompt = "Scrivi un semplice script Python che fa una divisione per zero. Rispondi solo con il codice."
    else:
       prompt = f"Il tuo codice ha generato questo errore: {errore_precedente}. Per favore, correggilo e rimandami il codice giusto."
 

    # Salvo la stringa in una variabile
    testo_grezzo = chat.send_message(prompt,).text
    testo_pulito = testo_grezzo.replace("```python", "").replace("```", "").strip()
    with open("script_generato.py", "w") as file_python:
        file_python.write(testo_pulito)
    risultato = subprocess.run(["python", "script_generato.py"], capture_output=True, text=True)
    
    # 3. VERDETTO
    if risultato.returncode == 0:
        print("Il codice è stato eseguito correttamente!")
        break
    else:
        print("Il codice ha generato un errore.")
        # Salviamo lo stderr rosso per la prossima prova
        errore_precedente = risultato.stderr
    numero_tentativi+=1
if numero_tentativi > 3:
    print("\nL'IA non è riuscita a risolvere il problema in 3 tentativi.")
