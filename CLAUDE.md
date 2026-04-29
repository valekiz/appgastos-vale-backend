# MapleGastos — guía para Claude

PWA de seguimiento de gastos personales con tema Maplestory + glassmorphism gamer. Lee cartolas de Santander Chile (CC y TC) desde Gmail, las parsea y muestra dashboards mensuales/anuales.

## Arquitectura (3 servicios desacoplados)

```
[ iPhone PWA ]  ⇄  [ Frontend GH Pages ]  ⇄  [ Backend Render ]  ⇄  [ Supabase Postgres ]
                                                    ↑
                                              [ Gmail IMAP ]  ← cartolas Santander
```

- **Frontend** — static, vanilla JS modules, vive en `frontend/`
  - Repo: `https://github.com/AndresKillinger/appgastos-frontend`
  - Branch: `main`
  - URL prod: `https://andreskillinger.github.io/appgastos-frontend/`
  - Sirve la PWA + Service Worker. Sin build step.

- **Backend** — FastAPI + SQLAlchemy, vive en `backend/`
  - Repo: `https://github.com/AndresKillinger/appgastos-backend`
  - Branch: `master` (no `main`, importante)
  - URL prod: `https://appgastos-backend.onrender.com`
  - API base: `/api/v1`
  - Render free tier: duerme tras 15 min idle, ~30s cold start
  - Env vars en Render: `DATABASE_URL`, `GMAIL_USER`, `GMAIL_APP_PASS`, `PDF_RUT`

- **DB** — Supabase Postgres (free tier)
  - Pausa tras 7 días sin queries → mantenerla activa con uptime ping al backend
  - Local dev usa SQLite (`backend/gastos.db`)

## Convenciones de datos (no obvias)

- **`categoria_id = 0`** en filtros API = "sin categoría" (NULL en DB). En el código se traduce con `categoria_id == None`.
- **`es_gasto = False`** marca categorías que NO cuentan como gasto neto (ej: pago de TC, sueldo). Sus cargos van a `total_excluido`, sus abonos NO restan del gasto.
- **Gasto neto** = `total_cargos - total_abonos` donde ambos excluyen categorías con `es_gasto=False`.
- **Abonos en una categoría** restan del total de esa categoría (ej: si Deporte tiene $100k de cargos y $30k de abono, muestra $70k).
- **`cuenta`** del movimiento: `'cuenta-corriente'`, `'tarjeta-credito'`, `'apple-pay'`. El filtro `tc` agrupa `apple-pay` + `tarjeta-credito`.
- **Periodo TC** ≠ mes calendario. Ciclo Santander suele ser ~26 a ~24 del siguiente mes.

## Endpoints clave del backend

| Endpoint | Método | Notas |
|---|---|---|
| `/health` | GET | Healthcheck — usar para uptime ping |
| `/sync` | POST | Lee Gmail, parsea PDFs nuevos, guarda movs |
| `/movements` | GET | Filtros: `desde`, `hasta`, `tipo`, `cuenta`, `buscar`, `categoria_id`, `limite` |
| `/movements/{id}/category` | PATCH | Asigna categoría a un movimiento |
| `/movements/credit-card` | POST | Carga manual de cargo TC |
| `/movements/apple-pay` | POST | Llamado por Atajo de iOS al pagar con Apple Pay |
| `/movements/dedupe` | POST | Borra duplicados (mismo fecha+desc+monto+cuenta), conserva id menor. `dry_run=true` para preview |
| `/categories` | GET/POST | Listar/crear |
| `/categories/{id}` | DELETE | Borra cat custom; movs quedan sin cat |
| `/cartolas/{id}` | DELETE | Borra cartola y sus movs |
| `/summary` | GET | Resumen mes: cargos, abonos, gasto_neto, by_category, top10 |
| `/summary/yearly` | GET | Totales por mes (cargos − abonos, excluye no-gasto) |
| `/summary/yearly/categories` | GET | Datos para gráfico apilado |
| `/upload-pdf` | POST | Sube PDF directo (puede dar OOM en Render free) |
| `/import-movements` | POST | Toma JSON ya parseado — usar este si `/upload-pdf` falla |

## Workflows comunes

### Subir cartolas históricas que no llegaron por Gmail
`/upload-pdf` suele dar 500 (OOM, Render free tier). Mejor: parsear local y POSTear el JSON a `/import-movements`.

```python
from app.parsers.pdf_parser import CartolaTCParser  # o CartolaCCParser
import json, urllib.request
parser = CartolaTCParser(rut='19322966')
with open('cartola.pdf','rb') as f:
    cartola = parser.parse(f.read())
body = cartola.to_dict()
body['cuenta'] = body.get('cuenta') or 'tarjeta-credito'
body['titular'] = body.get('titular') or 'Desconocido'
req = urllib.request.Request(
    'https://appgastos-backend.onrender.com/api/v1/import-movements',
    data=json.dumps(body).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
with urllib.request.urlopen(req, timeout=120) as r:
    print(r.read().decode())
```

### Forzar refresh del PWA en iPhone
Bumpear la constante `CACHE` en `frontend/sw.js` (`appgastos-vN` → `vN+1`). Al pushear, el SW detecta el cambio, borra caches viejas y bajan archivos nuevos. El usuario abre/cierra la app un par de veces.

### Cambiar el theme/categorías default
- Categorías default → `backend/app/models/database.py` función `init_db()`
- Theme PWA → `frontend/index.html` `<meta name="theme-color">` y `frontend/manifest.json` `theme_color/background_color`

## Stack del frontend

- **No bundler.** ES Modules nativos. `<script type="module" src="./js/app.js">`.
- **Sin framework.** Vanilla JS, render con template strings + innerHTML.
- **Estilo:** Press Start 2P para títulos/números, VT323 para texto. Glassmorphism con `backdrop-filter: blur(14px)` + gradients.
- **Mobs Maplestory:** se cargan desde `https://maplestory.io/api/GMS/latest/mob/{id}/render/stand`. `onerror` los oculta.
- **Custom dropdown** para mes/año (NO `<select>` nativo — iOS ignora fonts/colores en select).
- **Paleta de colores fallback** en `pickColor(cid, color)` para categorías sin color custom.
- **Service Worker** con strategy "network-first → fallback cache". Rutas relativas (`./`) para que funcione en `/appgastos-frontend/` de GH Pages.

## Stack del backend

- **FastAPI** + SQLAlchemy 2.x
- **Modelos**: `Categoria`, `CartolaProcesada`, `MovimientoCC` (en `app/models/database.py`)
- **Parsers**: `CartolaCCParser` (cuenta corriente) y `CartolaTCParser` (tarjeta crédito) en `app/parsers/pdf_parser.py`. Usan `pdfplumber` + extracción coordinada.
- **Email**: `app/services/email_poller.py` (IMAP a Gmail con App Password).

## Gotchas conocidos

- **`email_uid` es VARCHAR(64).** Si el UID generado supera 64 chars, INSERT falla con 500 sin detalle JSON. El endpoint `/import-movements` ahora trunca con `[:64]`.
- **Render free tier OOM.** PDFs grandes (~270KB) crashean al parsear server-side. Usar `/import-movements` con parseo local.
- **Render duerme.** 15 min idle → cold start. Configurar uptime ping a `/health` cada 10 min.
- **Supabase pausa tras 7 días sin queries.** Mismo ping resuelve.
- **iOS Safari `<select>`.** Ignora `font-family`, `color`, `background`. Por eso usamos dropdown custom.
- **PWA en GH Pages subdir.** `manifest.json` debe tener `start_url: "./"` (relativo) y `scope: "./"`. Si pones `/`, el ícono lleva al root de github.io (404).
- **Maplestory.io CORS-free pero a veces lenta.** Imágenes con `onerror="this.style.display='none'"` para fallback silencioso.

## Comandos útiles

```bash
# Backend local
cd backend
python -m venv venv && source venv/bin/activate  # o venv\Scripts\activate en Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Frontend local
cd frontend
python -m http.server 3000

# Push frontend
cd frontend && git push origin main

# Push backend (rama master)
cd backend && git push origin master
```

## Estructura de carpetas

```
AppGastos/
├── CLAUDE.md          ← este archivo
├── backend/           ← FastAPI (repo git separado, branch master)
│   ├── app/
│   │   ├── api/routes.py        ← endpoints
│   │   ├── models/database.py   ← SQLAlchemy + init_db()
│   │   ├── parsers/pdf_parser.py
│   │   └── services/email_poller.py
│   ├── requirements.txt
│   └── .env (gitignored)
└── frontend/          ← static (repo git separado, branch main)
    ├── index.html     ← todo el CSS inline aquí
    ├── manifest.json
    ├── sw.js
    ├── js/
    │   ├── api.js     ← BASE URL del backend
    │   └── app.js     ← UI logic
    └── icons/
```

## Cuando retomes este proyecto en una sesión nueva

1. Lee este archivo (Claude lo carga automático)
2. Si necesitas el estado más reciente: `git log --oneline -20` en cada repo
3. Para verificar que prod está vivo: `curl https://appgastos-backend.onrender.com/api/v1/health`
4. Las credenciales (Render, Supabase, Gmail App Password) están solo en mi password manager — no en el repo.
