# Daemon / Bounce (Notion watcher + URL forwarder)

Proyecto mínimo y desacoplado para acompañar a vuestro **Bridge**.

## Qué hace
- Mira la página `test_bridge` de Notion (o una página configurada)
- Extrae URLs del contenido
- **Cola simple de 1**: procesa solo la **primera URL válida**
- Hace *forward* GET de esa URL al Bridge (u otro destino)
- Tiene **dedupe por firma de página** y **anti-loop** por `_bounce_hop`
- Incluye `warmup_tick` opcional (p. ej. para Groq cada ~25 min)

## Endpoints
- `GET /health` – salud básica
- `GET /tick` – una iteración del watcher de Notion
- `GET /bounce?url=...` – rebote manual de una URL
- `GET /state` – estado interno (dedupe, últimos resultados)
- `GET /warmup_tick` – tick de warmup con cooldown

## Auth
Si `DAEMON_TOKEN` está definido, exige `?token=...` o header `X-DAEMON-TOKEN`.

## Deploy rápido (Render u otro)
- Python + `gunicorn`
- `requirements.txt` + `Procfile` incluidos

## Variables de entorno importantes
- `NOTION_TOKEN` (obligatoria para `/tick`)
- `NOTION_WATCH_PAGE_ID` **o** `NOTION_WATCH_PAGE_TITLE` (default `test_bridge`)
- `DAEMON_TOKEN` (opcional pero recomendado)
- `STATE_FILE` (default `/tmp/daemon_bounce_state.json`)
- `GROQ_WARMUP_URL` (opcional para `/warmup_tick`)

## Ejemplos
### Tick normal (cron/scheduler)
```bash
curl "https://tu-daemon.onrender.com/tick?token=TU_DAEMON_TOKEN"
```

### Tick debug (ver detalles)
```bash
curl "https://tu-daemon.onrender.com/tick?debug=1&token=TU_DAEMON_TOKEN"
```

### Dry-run (no reenvía)
```bash
curl "https://tu-daemon.onrender.com/tick?run=0&debug=1&token=TU_DAEMON_TOKEN"
```

### Rebote manual
```bash
curl "https://tu-daemon.onrender.com/bounce?url=https%3A%2F%2Ftu-bridge.onrender.com%2Fnotion%2Frelay_test_bridge%3Frun%3D1%26token%3D...&token=TU_DAEMON_TOKEN"
```

### Warmup Groq cada ~25 min
Configura `GROQ_WARMUP_URL` y llama con cron:
```bash
curl "https://tu-daemon.onrender.com/warmup_tick?token=TU_DAEMON_TOKEN"
```

## Comportamiento de dedupe
El daemon calcula una firma usando:
- `page_id`
- `last_edited_time` de Notion
- primera URL encontrada

Solo reenvía si esa firma cambia (o `force=1`).

## Anti-loop
Antes de reenviar una URL, añade `_bounce_hop=1`.
Si la URL ya trae `_bounce_hop` y supera el límite (`BOUNCE_MAX_HOPS`, default `1`), se bloquea.

## Recomendación de arquitectura
- **Bridge** = motor (LLM, GitHub, Notion response, etc.)
- **Daemon/Bounce** = watcher + transporte/rebote
- **Scheduler externo** = llama `/tick` y `/warmup_tick`

