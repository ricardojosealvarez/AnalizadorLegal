# Analizador Legal AEPD

Aplicacion local para explorar semanticamente informes juridicos de la AEPD.

El corpus PDF se mantiene fuera de este repositorio y se lee por defecto desde:

```text
/Users/ricardo/Downloads/informes-juridicos
```

## Que hace ahora

- Extrae texto de PDFs con `pypdf`.
- Indexa documentos y fragmentos en SQLite.
- Usa FTS5 para busqueda semantica ligera.
- Clasifica por reglas:
  - materia
  - base legal
  - regimen
  - senales de cambio doctrinal
- Muestra evolucion anual, temas frecuentes, temas escasos y fichas por informe.
- Muestra resumen cacheado de cada dictamen, puntos principales y conclusiones.
- Puede mejorar los resumenes con NVIDIA y conservarlos en SQLite para no repetir costes.
- Pagina los resultados de busqueda.

## Datos incluidos para despliegue

El repositorio puede incluir un snapshot comprimido de la base:

```text
data/aepd_reports.sqlite.gz
```

Al arrancar el servidor, si `data/aepd_reports.sqlite` no existe y el `.gz` si existe, la app lo descomprime automaticamente. Esto permite desplegar la app online sin subir el SQLite sin comprimir.

Nota: los PDFs completos originales no se incluyen en este repositorio por tamano. La busqueda, fragmentos y resumen funcionan con la base SQLite; el boton de PDF entero requiere que los PDFs existan en la ruta configurada o que se adapten a almacenamiento externo.

## Ejecutar con el runtime bundled de Codex

```bash
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer ingest
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer summarize
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer summarize-llm --limit 5
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer summarize-nvidia --limit 5
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer summarize-nvidia-auto --max-attempts 12 --max-successes 6
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer serve --port 8000
```

La capa LLM principal usa `NVIDIA_API_KEY` desde el entorno o desde `.env.local`.
El modelo puede configurarse con `NVIDIA_SUMMARY_MODEL`; por defecto se usa
`mistralai/mistral-medium-3.5-128b`. Los resumenes se generan con fragmentos
juridicamente relevantes y solo reemplazan la version anterior cuando la llamada
finaliza correctamente.

El endpoint de NVIDIA puede sobreescribirse con `NVIDIA_CHAT_COMPLETIONS_URL`.
Si prefieres declarar solo la base, usa `NVIDIA_API_BASE_URL` y la app
construira `/v1/chat/completions` automaticamente. Si el runtime no logra
resolver el host o el endpoint esta mal configurado, el lote automatico se
detiene pronto con un `stop_reason` explicito para evitar decenas de intentos
inutiles.

Si un informe tenia un resumen principal generado con OpenAI, se conserva como
variante en `summary_variants` antes de reemplazarlo por NVIDIA, para mantener
la comparacion historica.

Para automatizar lotes prudentes con NVIDIA, usa `summarize-nvidia-auto`. El
comando registra cada intento en `data/nvidia_summary_runs.jsonl`, corta tras
timeouts consecutivos, limita intentos y exitos, y actualiza automaticamente el
snapshot `data/aepd_reports.sqlite.gz` cuando guarda nuevos resumenes:

```bash
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer summarize-nvidia-auto --max-attempts 12 --max-successes 6 --timeout 90 --sleep 10 --stop-after-timeouts 2
```

Despues abre:

```text
http://127.0.0.1:8000
```

## Probar rapido con pocos PDFs

```bash
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer ingest --limit 100
/Users/ricardo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m legal_analyzer serve
```

## Ejecutar con un entorno propio

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m legal_analyzer ingest
python -m legal_analyzer summarize
python -m legal_analyzer serve
```

## Despliegue online

La app esta preparada para servicios como Render, Railway o Fly.io:

- `Procfile` para plataformas Python.
- `Dockerfile` para despliegue en contenedor.
- `render.yaml` como plantilla para Render.

En despliegue, el servidor usa la variable `PORT` si la plataforma la define:

```bash
python -m legal_analyzer serve --host 0.0.0.0
```

## Siguiente capa recomendada

Esta version evita dependencias externas para que el corpus pueda explorarse ya. La siguiente evolucion natural es sustituir o complementar FTS5 con embeddings juridicos, y guardar vectores en `pgvector`, Qdrant o SQLite con una extension vectorial.
