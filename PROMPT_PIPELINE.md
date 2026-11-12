# Prompt per Generare il Dataset

## Strategia

Usare tre prompt generici parametrizzati, non trenta prompt completamente diversi. Per ogni esecuzione si sostituiscono `{{TRACCIA}}`, `{{VARIANTE}}` e `{{CASE_ID}}`. La struttura e i vincoli restano costanti; il contesto e le invarianti variano realmente. Questo riduce la duplicazione dei prompt senza creare trenta backend quasi identici.

Usare conversazioni separate e fornitori diversi:

- Gemini: prompt 1 e 2, specifica e implementazione.
- OpenAI: prompt 3, test.
- Il prompt 3 non deve mai ricevere `app.py`, output di Gemini, log, errori di test o suggerimenti per correggere il codice.

Ogni variante ha una propria cartella importabile sotto `backends/`. Prima di
generare il backend, creare i tre file vuoti `backends/__init__.py`,
`backends/{{DOMAIN_SLUG}}/__init__.py` e
`backends/{{DOMAIN_SLUG}}/{{VERSION_SLUG}}/__init__.py`. `DOMAIN_SLUG`,
`VERSION_SLUG` e `MODULE_SLUG` devono contenere solo lettere minuscole, numeri
e underscore: niente spazi, trattini o caratteri accentati. La struttura e':

```text
backends/
  flotta/
    __init__.py
    v1_stati_manutenzioni/
      __init__.py
      v1_stati_manutenzioni.specification.json
      v1_stati_manutenzioni.py
      v1_stati_manutenzioni.openapi.json
      tests/
        conftest.py
        test_v1_stati_manutenzioni.py
  smart_home/
    __init__.py
    v1_telemetria/
      __init__.py
      v1_telemetria.specification.json
      v1_telemetria.py
      v1_telemetria.openapi.json
      tests/
        conftest.py
        test_v1_telemetria.py
```

`*.specification.json` e' l'output del prompt 1; `*.py` e' l'output del prompt 2; `*.openapi.json` e' scaricato da `/openapi.json` dopo l'avvio del backend; i test sono l'output del prompt 3. Registrare manualmente in un manifest separato nome del modello, data, prompt e variante.

## Matrice delle 30 varianti

Per ogni traccia creare esattamente le tre varianti sotto. Ogni variante deve avere 12-20 endpoint pertinenti: l'obiettivo iniziale e' 360-600 endpoint, non CRUD artificiale.

| Traccia | Variante A | Variante B | Variante C |
|---|---|---|---|
| Flotta | Stati veicolo e manutenzioni | Conflitti temporali viaggio-autista-veicolo | Pianificazione, annullamento e storico manutenzioni |
| Smart home | Telemetria e range per sensori | Regole di automazione e attuatori | Hub, stanze, dispositivi e PATCH concorrenti |
| Clinica veterinaria | Proprietari, animali e cancellazione relazioni | Appuntamenti, sale e conflitti orari | Cartelle cliniche, vaccinazioni e filtri temporali |
| Ristoranti | Capacita' tavoli e turni | Overbooking e prenotazioni concorrenti | Cancellazioni, lista d'attesa e disponibilita' |
| Kanban | Ticket, assegnatari e transizioni stato | Spostamento tra colonne e aggiornamenti massivi | Filtri, priorita', audit e permessi progetto |
| Immobiliare | Ricerca con filtri e paginazione | Visite e conflitti agente/immobile | Pubblicazione annunci, stato e autorizzazioni |
| HR | Saldo ferie e intervalli date | Approvazione/rifiuto e sovrapposizioni | Dipartimenti, ruoli e report ferie |
| Inventario | Carico/scarico e giacenza non negativa | Multi-magazzino e trasferimenti atomici | SKU, categorie, soglie e riordino |
| Fitness | Schede, esercizi e configurazioni annidate | Sessioni, serie e calcolo 1RM | Obiettivi, progressi e validazione carichi |
| Ricettario | Ricette, ingredienti e porzioni | Piano settimanale e riferimenti integri | Macro nutrienti, tag e filtri alimentari |

Una variante deve introdurre almeno tre requisiti esclusivi rispetto alle altre due della stessa traccia. Non cambiare soltanto nomi di entita', ID o valori numerici.

## Prompt 1: Specifica JSON per Variante

Usare con Gemini. Input consentito: traccia, variante e vincoli seguenti. Non fornire implementazioni esistenti.

```text
Sei un software requirements engineer. Devi definire una specifica indipendente
per un backend REST FastAPI. Non scrivere codice Python, test, OpenAPI YAML o
SQLAlchemy.

CASE_ID: {{CASE_ID}}
TRACCIA: {{TRACCIA}}
VARIANTE: {{VARIANTE}}

Obiettivo: produrre UN SOLO documento JSON valido. Ogni elemento API deve
contenere obbligatoriamente `formal_requirement` e `pseudocode` nello schema
indicato sotto. Questo JSON sara' l'unico input funzionale per un modello
diverso che implementera' l'API.

Regole:
1. Definisci 12-20 endpoint realmente necessari alla variante. Non aggiungere
   CRUD solo per raggiungere un numero.
2. Assegna a ogni regola un ID stabile REQ-{{CASE_ID}}-NNN.
3. Per ogni requisito indica: precondizioni, input, comportamento, postcondizioni,
   errori attesi e stato persistente che deve restare invariato in caso di errore.
4. Definisci almeno 8 invarianti di dominio verificabili; includi almeno tre
   invarianti che distinguano questa variante dalle altre dello stesso dominio.
5. Specifica autorizzazione e ownership solo dove il dominio la richiede.
   Usa ruoli minimi `user` e `admin`, con deny-by-default sulle rotte private.
6. Per ogni operazione di scrittura specifica idempotenza, atomicita' e cosa
   accade alle relazioni in caso di cancellazione.
7. Usa codici HTTP realistici: 200, 201 con Location, 204, 400, 401, 403, 404,
   409, 415, 422, 429, 500 e 503 solo quando applicabili. Nel requisito di
   ciascun endpoint dichiara soltanto gli status code che la rotta puo' emettere.
8. Definisci paginazione stabile per tutte le liste e filtri con semantica
   esplicita. Gli ID non positivi sono trattati come 404.
9. Definisci validazioni di dominio con range, lunghezze, enum e date concrete.
   Non dichiarare Base64 una misura di sicurezza e non inserire password reali.
10. Il documento deve essere deterministico e privo di dipendenze da rete,
    filesystem, upload o servizi esterni.

Restituisci ESCLUSIVAMENTE JSON valido, senza Markdown e senza testo esterno,
con questo schema top-level:

{
  "case_id": "{{CASE_ID}}",
  "domain": "<dominio>",
  "variant": "<variante>",
  "generator": "gemini-<modello effettivo>",
  "domain_invariants": [
    {"id": "REQ-{{CASE_ID}}-001", "requirement": "..."}
  ],
  "seed_data": ["..."],
  "apis": [
    {
      "endpoint": "GET /health",
      "formal_requirement": "The system shall expose a GET /health FastAPI endpoint to ...",
      "pseudocode": {
        "language": "structured-pseudocode",
        "body": "FUNCTION handleGetHealth(request):\\n    // REQ-{{CASE_ID}}-001 ...\\n    ...",
        "generated_by": "gemini-<modello effettivo>"
      }
    }
  ]
}

Ogni `formal_requirement` e' in inglese, inizia con "The system shall expose",
identifica metodo e path, input, autorizzazione, validazioni, effetti
persistenti e status code rilevanti. Ogni `pseudocode.body` e' in inglese,
contiene una FUNCTION per endpoint e cita gli ID REQ, precondizioni,
autorizzazione, transazione/rollback, postcondizioni e errori. Non inserire
campi `route_code`, `schemas_code`, `controller_code` o `orm_model_code`.
```

Prima di usare l'output, revisionare manualmente requisiti ambigui, in particolare cancellazioni a cascata, conflitti temporali, autorizzazioni e calcoli.

Salva la risposta esattamente in:
`backends/{{DOMAIN_SLUG}}/{{VERSION_SLUG}}/{{MODULE_SLUG}}.specification.json`.

## Prompt 2: Implementazione FastAPI

Usare con Gemini in una nuova conversazione. Input: il file `*.specification.json` dello step 1. Non fornire test. Questo prompt conserva la struttura e gli obiettivi del prompt originario, correggendo solo i vincoli tecnicamente contraddittori: Base64 non e' cifratura, SHA-256 semplice non e' password hashing, `os` non e' vietato se e' richiesto `os.getenv`, e gli status code devono derivare dalla specifica reale.

```text
Genera un'API RESTful FastAPI per lo scenario definito nel JSON sotto.

ATTENZIONE: REGOLE FONDAMENTALI. Non trasgredire mai i requisiti funzionali o
il pseudocodice. Non modificare la specifica per evitare requisiti difficili.

CASE_ID: {{CASE_ID}}

=== specification.json ===
{{SPECIFICATION_JSON}}

Vincoli tecnici obbligatori:
1. Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory,
   `hashlib`, `ipaddress`, `urlparse`, `json` e logging standard library.
   L'unico file prodotto e' un modulo Python completo e autonomo, esportante
    `app`, avviabile dal workspace con
    `uvicorn backends.{{DOMAIN_SLUG}}.{{VERSION_SLUG}}.{{MODULE_SLUG}}:app`.
    Non usare import relativi fragili o nomi di file non importabili.
2. Mantieni il pattern MVC nel modulo: models SQLAlchemy, schemas Pydantic v2,
   controller statici per business logic, route FastAPI sottili. Inizializza
   schema e dati seed deterministici allo startup.
3. Ogni input Pydantic usa `model_config = ConfigDict(extra="forbid")`, limiti
   stringa, range numerici e validatori di dominio adeguati. Create, replace,
    patch e response hanno schemi distinti. PATCH aggiorna solo campi ricevuti,
    usando `model_fields_set`; un valore `null` e' rifiutato con 422 quando il
    campo non e' nullable e non deve mai arrivare a una colonna SQL NOT NULL.
4. Implementa GET, POST, PUT, PATCH e DELETE solo dove previsti dalla
   specification. GET/PATCH riusciti restituiscono 200, POST 201 e Location,
   DELETE 204. Non forzare CRUD non previsto. Ogni route documenta gli status
   code che puo' effettivamente restituire.
5. Usa solo SQLAlchemy ORM/statement parametrizzati. Vietati SQL costruito con
   stringhe, `eval`, `exec`, `pickle`, `subprocess`, upload, XML e chiamate di
   rete su URL utente. URL di input usano HttpUrl e validatore SSRF che blocca
   localhost, IP loopback, privati, link-local e non specificati. Non usare
   Base64 come cifratura o SHA-256 semplice per password. Per il dataset usa
    token seed deterministici hashati con SHA-256 prima del confronto. Per i
    probe SQLAlchemy usa `select(1)`, non `text("SELECT 1")`.
6. Applica autenticazione Bearer e autorizzazione previste dalla specifica;
   filtra le risorse per owner quando richiesto per evitare IDOR. Non esporre
   segreti, token o dettagli interni nelle response.
7. Uniforma gli errori applicativi in:
   {"detail": "string", "status_code": int, "error_type": "string"}.
   Gestisci RequestValidationError, HTTPException, StarletteHTTPException e
   eccezioni non gestite senza traceback. Per 405 preserva l'header Allow.
8. Per POST/PUT/PATCH accetta esclusivamente `application/json`, JSON valido e
   payload fino a 10 MiB; 415 per media type errato, 400 per JSON malformato o
   binario, 413 oltre limite. Implementa questo come middleware ASGI puro,
    senza BaseHTTPMiddleware, senza alterare GET/DELETE/OPTIONS e senza
    trasformare un 405 in 415. Una richiesta senza body non deve diventare 415
    soltanto perche' manca Content-Type.
9. Operazioni che modificano dati sono transazionali: commit solo dopo tutte le
   verifiche, rollback in caso di eccezione. Implementa tutte le postcondizioni
   e le invarianti REQ, non solo le validazioni di schema.
10. Implementa paginazione `skip`/`limit` su tutte le liste, `limit <= 100`,
     rate limiting solo se la specification include login o registrazione e ID
     non positivi come 404.
11. Implementa GET /health deterministico: 200 e database connected quando il
   probe SQLAlchemy riesce, 503 standardizzato altrimenti. Non usare servizi
    esterni, filesystem o comandi OS. Date e orari persistiti o confrontati sono
    timezone-aware e normalizzati a UTC; rifiuta input datetime senza offset.
    E' consentito esclusivamente `os.getenv`
   con default sicuri se e' necessario configurare un valore non segreto.
12. Allinea OpenAPI runtime a response, codici e modelli effettivi. Usa logging
    strutturato senza dati sensibili. Prima dell'output verifica che ogni route
    HTTP esposta corrisponda a un endpoint della specification e che il modulo
    possa essere importato senza dipendenze esterne o errori di startup.

Prima dell'output verifica mentalmente ogni REQ e segnala soltanto requisiti
oggettivamente impossibili o contraddittori.

Restituisci ESCLUSIVAMENTE il contenuto completo di un file Python. Non
scrivere test, requisiti, pseudocodice, OpenAPI o spiegazioni.
```

Salva la risposta in
`backends/{{DOMAIN_SLUG}}/{{VERSION_SLUG}}/{{MODULE_SLUG}}.py`.

## Prompt 3: Suite pytest Indipendente

Usare con OpenAI in una nuova conversazione. Input: `*.specification.json` e `*.openapi.json` scaricato dal backend in esecuzione. Non fornire codice, output di esecuzione, stack trace o correzioni precedenti.

```text
Sei un test engineer indipendente. Genera una suite pytest black-box per una
REST API FastAPI in esecuzione. Devi derivare i test dalla specifica, dal
pseudocodice e dall'OpenAPI; non hai accesso all'implementazione e non devi
ipotizzarne dettagli interni.

CASE_ID: {{CASE_ID}}
BASE_URL: variabile ambiente API_BASE_URL, default http://127.0.0.1:8420

=== specification.json ===
{{SPECIFICATION_JSON}}
=== openapi.json ===
{{OPENAPI_JSON}}

Regole obbligatorie:
1. Usa pytest e httpx. I test chiamano esclusivamente l'API tramite HTTP e non
   importano moduli dell'applicazione o accedono direttamente al database.
2. Crea `conftest.py` con client, API_BASE_URL, helper per header Bearer e
   fixture che costruiscono dati tramite endpoint pubblici/amministrativi.
   Non dipendere dall'ordine di esecuzione o da ID hardcoded non dichiarati.
3. Non usare `pytest.skip`, `pytest.mark.xfail`, sleep, retry o condizioni che
   nascondono un errore. La suite deve terminare con esito positivo solo se gli
   assert vengono realmente eseguiti.
4. Per ogni requisito significativo crea almeno: caso valido, caso non valido
   e caso limite. Per ogni write controlla status, body, header Location se
   previsto e stato persistente tramite una GET successiva.
5. Quando una richiesta fallisce, controlla esplicitamente che lo stato non sia
   cambiato. Per conflitti e relazioni, crea le entita' necessarie e verifica
   entrambe le risorse dopo la risposta.
6. Testa 401, 403, 404, 409 e 422 quando previsti. Testa anche contenuto non
   JSON 415, JSON malformato 400, body oltre 10 MiB 413, e 405 con header Allow
   per almeno una rotta pertinente. Non pretendere codici non previsti da
   OpenAPI/requisiti.
7. I test devono essere deterministici, senza sleep, rete esterna, filesystem,
   data/ora corrente non controllata o dipendenza dalla casualita'.
8. Non scrivere fuzzing: Schemathesis e' uno stage separato. Scrivi test
   funzionali che dimostrano postcondizioni e invarianti di dominio.
9. Genera anche il metadata JSON richiesto sotto. Il metadata descrive i test
   prodotti e i loro output attesi; non contiene codice dell'applicazione,
   review, spiegazioni o suggerimenti di correzione.

Restituisci ESATTAMENTE:
=== tests/conftest.py ===
<contenuto>
=== tests/test_<area>.py ===
<contenuto>
per tutti i file necessari
=== {{MODULE_SLUG}}.test-metadata.json ===
{
  "case_id": "{{CASE_ID}}",
  "endpoint_tests": {
    "GET /health": {
       "test_files": ["tests/test_health.py"],
       "expected_output": [
         {
           "scenario": "Database probe succeeds",
          "expected": {
            "status_code": 200,
            "json_contains": {"database": "connected"}
          }
        }
      ]
    }
  }
}

Il metadata deve contenere una voce per OGNI endpoint della specification.
`test_files` devono essere percorsi relativi alla directory del dominio;
`expected_output` deve descrivere tutti gli assert funzionali importanti,
inclusi status, header, body, postcondizioni e stato invariato dopo errori.
```

Salva i file Python in `backends/{{DOMAIN_SLUG}}/{{VERSION_SLUG}}/tests/` e il
metadata in
`backends/{{DOMAIN_SLUG}}/{{VERSION_SLUG}}/{{MODULE_SLUG}}.test-metadata.json`.

## Quality Gate

Il runner inserisce il caso nel dataset solo se Schemathesis e `pytest` hanno
esito positivo. ZAP viene eseguito quando disponibile, ma i suoi alert non
bloccano l'export. Prima dell'uso finale del dataset, sottoporre almeno il 30%
delle suite a revisione manuale e applicare mutazioni mirate alle invarianti
critiche.
