# Roblox Zombie AI Learning Service

Backend seguro para recibir telemetria de combate desde Roblox y publicar una
configuracion de IA validada. La version 4 separa dos conceptos que antes se
mezclaban:

- **candidate**: aprende de eventos nuevos, pero nunca afecta automaticamente a
  partidas activas;
- **production snapshot**: unica configuracion que Roblox descarga. Solo cambia
  mediante una promocion administrativa y puede revertirse.

Esto evita que datos malos, duplicados o manipulados cambien todos los NPC en
tiempo real.

## Configuracion

Variables obligatorias en produccion:

| Variable | Uso |
|---|---|
| `DATABASE_URL` | PostgreSQL persistente. Koyeb recrea el disco local de una instancia. |
| `AI_API_KEY` | Secreto compartido para escrituras y rutas administrativas. |

Opcionales: `WRITE_RATE_LIMIT_PER_MINUTE` (120), `MAX_BATCH_SIZE` (100) y
`LEARNING_RATE` (0.12). Consulta [.env.example](.env.example).

Sin `AI_API_KEY`, las lecturas funcionan pero todas las escrituras responden
`503`; es un modo seguro para no aceptar aprendizaje anonimo. Sin
`DATABASE_URL`, el servicio usa SQLite para desarrollo local. SQLite solo es
apto en Koyeb si `SQLITE_PATH` apunta a un volumen persistente y se ejecuta una
sola replica.

## API

- `GET /api/v2/snapshot`: snapshot de produccion estable.
- `POST /api/v2/events/batch`: lote idempotente de eventos, protegido por
  `X-API-Key`.
- `GET /api/v2/admin/candidate`: inspeccion del candidato.
- `POST /api/v2/admin/promote`: promocion manual del candidato.
- `POST /api/v2/admin/rollback`: regreso a una version anterior.
- `PUT /api/v2/admin/runtime-config`: modificadores globales validados.
- `GET /api/v2/admin/export`: exportacion JSON autenticada.
- `GET /health/live` y `GET /health/ready`: probes de Koyeb.

Las rutas v1 de lectura y reporte siguen disponibles para migrar servidores de
Roblox gradualmente. El antiguo backup SQLite publico responde `410 Gone`.

Cada evento v2 necesita un UUID estable en `event_id`. Si Roblox reintenta el
mismo lote, el servidor lo reconoce como duplicado y no vuelve a entrenar.

## Desarrollo local

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
$env:AI_API_KEY = "development-secret"
.\.venv\Scripts\python -m uvicorn main:app --reload
```

Pruebas:

```powershell
.\.venv\Scripts\python -m pytest -q
```

## Despliegue seguro en Koyeb

1. Crea o conecta PostgreSQL y guarda su URL en `DATABASE_URL`.
2. Genera un secreto largo y distinto para `AI_API_KEY`.
3. Configura el health check HTTP en `/health/ready`.
4. Despliega primero una sola replica.
5. Verifica `/health/ready` y `/api/v1/zombie-brain-weights`.
6. Coloca el mismo secreto unicamente en el Script de servidor del bridge de
   Roblox. Nunca lo pongas en `ReplicatedStorage`, un LocalScript o la interfaz.
7. Cuando haya suficientes eventos reales, inspecciona candidate y promueve una
   revision. No automatices promociones sin evaluacion previa.

La API crea sus tablas al iniciar. Si encuentra la tabla SQLite v3
`zombie_weights`, importa sus datos una sola vez como candidato; no publica esos
pesos hasta una promocion.
