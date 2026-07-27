"""
Endpoints FastAPI para AppGastos.

POST /sync          — lee Gmail, procesa PDFs nuevos, guarda en DB
GET  /movements     — lista movimientos con filtros opcionales
GET  /summary       — resumen del mes (totales, top gastos)
GET  /health        — healthcheck para Railway
"""
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal


def _parse_monto_clp(v) -> int:
    """Parsea cualquier formato de monto chileno a entero positivo de pesos.
    Acepta: 1010, "1010", "$1.010", "1.010", "$1010", etc.
    Para los pesos chilenos asumimos que no hay decimales en uso normal.
    """
    if isinstance(v, (int, float)):
        return abs(int(round(v)))
    s = re.sub(r'[^\d-]', '', str(v))
    return abs(int(s)) if s else 0


# Stopwords y prefijos que el banco antepone — los strippeamos para comparar
_DESC_NOISE = {
    'santiago', 'las', 'condes', 'colchagua', 'cl', 'sa', 'spa', 'ltda',
    'compra', 'directa', 'pago', 'comercial', 'servicios',
}

def _normalize_desc(s: str) -> set[str]:
    """Tokeniza una descripción para comparar similitud. Strippea ruido del banco."""
    if not s:
        return set()
    # minúsculas, quitar acentos sencillos, quedarse solo con palabras
    s = s.lower()
    s = re.sub(r'[áä]', 'a', s); s = re.sub(r'[éë]', 'e', s)
    s = re.sub(r'[íï]', 'i', s); s = re.sub(r'[óö]', 'o', s); s = re.sub(r'[úü]', 'u', s)
    tokens = re.findall(r'[a-z0-9]+', s)
    return {t for t in tokens if len(t) >= 3 and t not in _DESC_NOISE}


def _desc_similarity(a: str, b: str) -> float:
    """Jaccard sobre tokens normalizados. 0 = nada en común, 1 = idéntico."""
    ta, tb = _normalize_desc(a), _normalize_desc(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, or_, func

from app.models.database import (
    CartolaProcesada, Categoria, MovimientoCC, get_session, init_db
)
from app.parsers.pdf_parser import CartolaCCParser, CartolaTCParser
from app.parsers.itau_parser import CartolaITAUCCParser, CartolaITAUTCParser
from app.services.email_poller import GmailPoller

logger = logging.getLogger(__name__)
router = APIRouter()


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_db():
    db = get_session()
    try:
        yield db
    finally:
        db.close()


def _save_cartola(db: Session, cartola_data: dict, email_uid: str) -> int:
    """Persiste una cartola y sus movimientos. Retorna el id de la cartola."""
    # Evitar duplicados
    existing = db.execute(
        select(CartolaProcesada).where(CartolaProcesada.email_uid == email_uid)
    ).scalar_one_or_none()
    if existing:
        return existing.id

    cp = CartolaProcesada(
        email_uid=email_uid,
        periodo=cartola_data.get("periodo"),
        cuenta=cartola_data.get("cuenta"),
        titular=cartola_data.get("titular"),
        saldo_inicial=cartola_data.get("saldo_inicial"),
        saldo_final=cartola_data.get("saldo_final"),
        desde=date.fromisoformat(cartola_data["desde"]) if cartola_data.get("desde") else None,
        hasta=date.fromisoformat(cartola_data["hasta"]) if cartola_data.get("hasta") else None,
        procesado_en=datetime.utcnow().isoformat(),
    )
    db.add(cp)
    db.flush()

    for mov in cartola_data.get("movimientos", []):
        m = MovimientoCC(
            cartola_id=cp.id,
            fecha=date.fromisoformat(mov["fecha"]),
            descripcion=mov["descripcion"],
            cargo=mov.get("cargo"),
            abono=mov.get("abono"),
            saldo=mov.get("saldo"),
            monto=mov["monto"],
            sucursal=mov.get("sucursal"),
            numero_doc=mov.get("numero_doc"),
            cuenta=cartola_data.get("cuenta"),
        )
        db.add(m)

    db.commit()
    return cp.id


# ── endpoints ─────────────────────────────────────────────────────────────────

class SetCategoriaBody(BaseModel):
    categoria_id: int | None = None


class CreditCardMovimiento(BaseModel):
    fecha: str
    descripcion: str
    monto: str | float | int
    tipo: str = "cargo"
    cuenta: str = "tarjeta-credito"


class ApplePayMovimiento(BaseModel):
    descripcion: str
    monto: str | float | int   # iOS Shortcuts puede mandar "$1.010", 1010, 1010.0...
    fecha: str | None = None   # opcional — si no viene, usa hoy
    cuenta: str = "apple-pay"


class ManualMovimiento(BaseModel):
    """Movimiento ingresado manualmente por el usuario antes que llegue la cartola.
    Se auto-fusiona con el cargo de la cartola correspondiente cuando esta entra."""
    descripcion: str
    monto: str | float | int
    fecha: str | None = None
    tipo: str = "cargo"   # 'cargo' o 'abono'
    cuenta: str = "manual"


@router.post("/movements/credit-card")
def add_credit_card_movement(body: CreditCardMovimiento, db: Session = Depends(_get_db)):
    """Agrega un movimiento de tarjeta de crédito manualmente."""
    monto_abs = _parse_monto_clp(body.monto)
    m = MovimientoCC(
        cartola_id=0,
        fecha=date.fromisoformat(body.fecha),
        descripcion=body.descripcion,
        cargo=monto_abs if body.tipo == "cargo" else None,
        abono=monto_abs if body.tipo == "abono" else None,
        monto=-monto_abs if body.tipo == "cargo" else monto_abs,
        cuenta=body.cuenta,
    )
    db.add(m)
    db.commit()
    return {"ok": True, "id": m.id, "fecha": body.fecha, "descripcion": body.descripcion, "monto": monto_abs}


# Buffer en memoria de últimos 50 payloads recibidos en /movements/apple-pay.
# Se pierde al reiniciar Render, pero sirve para debug en vivo del atajo iOS.
_recent_apple_pay_raws: list = []


@router.post("/movements/apple-pay")
async def add_apple_pay_movement(request: Request, db: Session = Depends(_get_db)):
    """Recibe transacciones desde el Atajo de iOS Apple Pay. Sin autenticación intencional (uso personal)."""
    raw_bytes = await request.body()
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    _recent_apple_pay_raws.append({"ts": datetime.utcnow().isoformat(), "raw": raw_text})
    if len(_recent_apple_pay_raws) > 50:
        _recent_apple_pay_raws.pop(0)

    try:
        import json as _json
        data = _json.loads(raw_text) if raw_text.strip() else {}
        body = ApplePayMovimiento(**data)
    except Exception as exc:
        logger.warning("Apple Pay body parse failed: %s — raw=%r", exc, raw_text[:500])
        raise HTTPException(status_code=400, detail=f"Body inválido: {exc}")

    monto_abs = _parse_monto_clp(body.monto)
    if monto_abs == 0:
        logger.warning("Apple Pay con monto=0. raw=%r desc=%r", body.monto, body.descripcion)
    fecha = date.fromisoformat(body.fecha) if body.fecha else date.today()
    m = MovimientoCC(
        cartola_id=0,
        fecha=fecha,
        descripcion=body.descripcion,
        cargo=monto_abs,
        monto=-monto_abs,
        cuenta=body.cuenta,
    )
    db.add(m)
    db.commit()
    return {"ok": True, "id": m.id, "descripcion": body.descripcion, "monto": monto_abs}


@router.post("/movements/manual")
def add_manual_movement(body: ManualMovimiento, db: Session = Depends(_get_db)):
    """Gasto/abono manual ingresado por el usuario antes que llegue la cartola.
    Se auto-fusiona con el cargo de la cartola correspondiente cuando esta entra
    (vía la lógica de merge en /sync)."""
    monto_abs = _parse_monto_clp(body.monto)
    fecha = date.fromisoformat(body.fecha) if body.fecha else date.today()
    es_abono = body.tipo == "abono"
    m = MovimientoCC(
        cartola_id=0,
        fecha=fecha,
        descripcion=body.descripcion,
        cargo=monto_abs if not es_abono else None,
        abono=monto_abs if es_abono else None,
        monto=-monto_abs if not es_abono else monto_abs,
        cuenta=body.cuenta or "manual",
    )
    db.add(m)
    db.commit()
    return {"ok": True, "id": m.id, "descripcion": body.descripcion, "monto": monto_abs, "tipo": body.tipo}


@router.get("/categories")
def list_categories(db: Session = Depends(_get_db)):
    """Lista todas las categorías disponibles."""
    cats = db.execute(select(Categoria).order_by(Categoria.id)).scalars().all()
    return [
        {
            "id": c.id,
            "nombre": c.nombre,
            "icono": c.icono,
            "mob_id": c.mob_id,
            "es_gasto": c.es_gasto,
            "color": c.color,
        }
        for c in cats
    ]


class CreateCategoriaBody(BaseModel):
    nombre: str
    icono: str = "❓"
    mob_id: int | None = None
    es_gasto: bool = True
    color: str = "#888888"


@router.post("/categories")
def create_category(body: CreateCategoriaBody, db: Session = Depends(_get_db)):
    """Crea una categoría personalizada."""
    cat = Categoria(
        nombre=body.nombre,
        icono=body.icono,
        mob_id=body.mob_id,
        es_gasto=body.es_gasto,
        color=body.color,
        es_sistema=False,
    )
    db.add(cat)
    db.commit()
    return {
        "id": cat.id,
        "nombre": cat.nombre,
        "icono": cat.icono,
        "mob_id": cat.mob_id,
        "es_gasto": cat.es_gasto,
        "color": cat.color,
    }


@router.delete("/categories/{cat_id}")
def delete_category(cat_id: int, db: Session = Depends(_get_db)):
    """Elimina una categoría personalizada y desasigna sus movimientos."""
    cat = db.get(Categoria, cat_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    if cat.es_sistema:
        raise HTTPException(status_code=400, detail="No se puede eliminar una categoría del sistema")
    for m in db.execute(select(MovimientoCC).where(MovimientoCC.categoria_id == cat_id)).scalars().all():
        m.categoria_id = None
    db.delete(cat)
    db.commit()
    return {"ok": True}


# Cuentas que son "tiempo-real" (provisorias, esperan match en cartola)
REALTIME_CUENTAS = ('apple-pay', 'manual')

def _merge_apple_pay_with_tc(
    db: Session,
    desde: date | None = None,
    fecha_window: int = 5,
    min_similarity: float = 0.20,
    dry_run: bool = False,
):
    """Para cada movimiento de tiempo-real (apple-pay o manual), busca un cargo
    equivalente en una cartola (mismo monto, fecha ±N días, descripción similar)
    y los fusiona: transfiere la categoría al movimiento de cartola y borra el provisorio.
    """
    if desde is None:
        desde = date.today() - timedelta(days=90)

    realtime_movs = db.execute(
        select(MovimientoCC)
        .where(
            MovimientoCC.cuenta.in_(REALTIME_CUENTAS),
            MovimientoCC.fecha >= desde,
        )
    ).scalars().all()

    merged = []
    for rt in realtime_movs:
        # Match: mismo monto, dentro de ±N días, en cualquier cuenta NO provisoria
        # (cartola CC, TC, o cualquier otra cuenta del banco)
        if rt.cargo:
            monto_filter = MovimientoCC.cargo == rt.cargo
        elif rt.abono:
            monto_filter = MovimientoCC.abono == rt.abono
        else:
            continue

        candidatos = db.execute(
            select(MovimientoCC).where(
                ~MovimientoCC.cuenta.in_(REALTIME_CUENTAS),
                monto_filter,
                MovimientoCC.fecha >= rt.fecha - timedelta(days=fecha_window),
                MovimientoCC.fecha <= rt.fecha + timedelta(days=fecha_window),
            )
        ).scalars().all()
        if not candidatos:
            continue

        scored = sorted(
            ((c, _desc_similarity(rt.descripcion, c.descripcion)) for c in candidatos),
            key=lambda x: x[1], reverse=True,
        )
        best, score = scored[0]
        # Si hay un solo candidato, lo aceptamos aunque no haya descripcion similar
        # (un cargo del mismo monto en ±5 días es muy probablemente el mismo).
        if len(candidatos) > 1 and score < min_similarity:
            continue

        info = {
            "realtime_id": rt.id,
            "realtime_cuenta": rt.cuenta,
            "cartola_id": best.id,
            "monto": str(rt.cargo or rt.abono or rt.monto),
            "rt_desc": rt.descripcion,
            "cartola_desc": best.descripcion,
            "score": round(score, 2),
        }
        merged.append(info)

        if not dry_run:
            # Si el de cartola no tiene categoría y el provisorio sí, transferimos
            if rt.categoria_id and not best.categoria_id:
                best.categoria_id = rt.categoria_id
            db.delete(rt)

    if not dry_run:
        db.commit()

    return merged


@router.post("/movements/merge-realtime")
def merge_realtime_with_cartola(
    desde: str | None = Query(None, description="Solo merge desde esta fecha YYYY-MM-DD (default: hace 90 días)"),
    dry_run: bool = Query(False),
    db: Session = Depends(_get_db),
):
    """Fusiona movimientos apple-pay con sus duplicados de la cartola TC.
    Útil correr después de un sync — se llama automáticamente desde /sync."""
    desde_d = date.fromisoformat(desde) if desde else None
    merged = _merge_apple_pay_with_tc(db, desde=desde_d, dry_run=dry_run)
    return {"merged_count": len(merged), "dry_run": dry_run, "details": merged[:50]}


@router.post("/movements/dedupe")
def dedupe_movements(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    dry_run: bool = Query(False, description="Si true, solo cuenta sin borrar"),
    db: Session = Depends(_get_db),
):
    """Borra duplicados (misma fecha+descripcion+monto+cuenta), conservando el id más bajo."""
    q = select(MovimientoCC).order_by(MovimientoCC.id)
    filters = []
    if desde:
        filters.append(MovimientoCC.fecha >= date.fromisoformat(desde))
    if hasta:
        filters.append(MovimientoCC.fecha <= date.fromisoformat(hasta))
    if filters:
        q = q.where(and_(*filters))
    movs = db.execute(q).scalars().all()

    seen: dict = {}
    to_delete: list = []
    for m in movs:
        key = (m.fecha.isoformat(), m.descripcion, str(m.monto), m.cuenta)
        if key in seen:
            to_delete.append(m)
        else:
            seen[key] = m.id

    deleted_ids = [m.id for m in to_delete]
    if not dry_run:
        for m in to_delete:
            db.delete(m)
        db.commit()

    return {
        "scanned": len(movs),
        "duplicates_removed": len(deleted_ids),
        "deleted_ids": deleted_ids[:50],
        "dry_run": dry_run,
    }


@router.get("/movements/apple-pay/debug")
def apple_pay_debug():
    """Devuelve los últimos payloads recibidos en /movements/apple-pay (en memoria, max 50)."""
    return {"count": len(_recent_apple_pay_raws), "raws": list(reversed(_recent_apple_pay_raws))}


class UpdateMovBody(BaseModel):
    descripcion: str | None = None
    monto: str | float | int | None = None
    fecha: str | None = None
    cuenta: str | None = None


@router.patch("/movements/{mov_id}")
def update_movement(mov_id: int, body: UpdateMovBody, db: Session = Depends(_get_db)):
    """Actualiza campos editables de un movimiento (monto, descripcion, fecha, cuenta)."""
    m = db.get(MovimientoCC, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    if body.descripcion is not None:
        m.descripcion = body.descripcion
    if body.fecha is not None:
        m.fecha = date.fromisoformat(body.fecha)
    if body.cuenta is not None:
        m.cuenta = body.cuenta
    if body.monto is not None:
        n = _parse_monto_clp(body.monto)
        # Para apple-pay y tarjeta-credito, todo es cargo (negativo)
        es_cargo = m.cuenta in ("apple-pay", "tarjeta-credito") or (m.cargo is not None) or (m.monto and int(m.monto) < 0)
        if es_cargo:
            m.cargo = n
            m.abono = None
            m.monto = -n
        else:
            m.abono = n
            m.cargo = None
            m.monto = n
    db.commit()
    return {"ok": True, "id": mov_id, "monto": str(m.monto), "descripcion": m.descripcion}


@router.delete("/movements/{mov_id}")
def delete_movement(mov_id: int, db: Session = Depends(_get_db)):
    """Borra un movimiento individual."""
    m = db.get(MovimientoCC, mov_id)
    if not m:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    db.delete(m)
    db.commit()
    return {"ok": True, "id": mov_id}


@router.delete("/cartolas/{cartola_id}")
def delete_cartola(cartola_id: int, db: Session = Depends(_get_db)):
    """Borra una cartola y todos sus movimientos."""
    cp = db.get(CartolaProcesada, cartola_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Cartola no encontrada")
    movs = db.execute(select(MovimientoCC).where(MovimientoCC.cartola_id == cartola_id)).scalars().all()
    for m in movs: db.delete(m)
    db.delete(cp)
    db.commit()
    return {"ok": True, "cartola_id": cartola_id, "movs_deleted": len(movs)}


@router.patch("/movements/{mov_id}/category")
def set_movement_category(
    mov_id: int,
    body: SetCategoriaBody,
    db: Session = Depends(_get_db),
):
    """Asigna una categoría a un movimiento."""
    mov = db.get(MovimientoCC, mov_id)
    if not mov:
        raise HTTPException(status_code=404, detail="Movimiento no encontrado")
    mov.categoria_id = body.categoria_id
    db.commit()
    return {"ok": True, "id": mov_id, "categoria_id": body.categoria_id}


@router.get("/sync/debug/search")
def debug_search(subject: str | None = Query(None, description="Subject a buscar (override del filtro)")):
    """Debug: muestra qué uids encuentra cada estrategia de búsqueda en Gmail."""
    try:
        poller = GmailPoller()
        return poller.debug_search(subject=subject)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error: {exc}")


@router.get("/sync/debug/inbox")
def debug_inbox(days: int = Query(60, le=365)):
    """Lista los últimos emails de Santander en la inbox (sin filtrar por subject).
    Sirve para diagnosticar qué subjects realmente están llegando."""
    try:
        poller = GmailPoller()
        subjects = poller.list_subjects(days=days)
        return {"count": len(subjects), "emails": subjects}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error conectando a Gmail: {exc}")


@router.get("/sync/debug/fetch-uids")
def debug_fetch_uids():
    """Diagnóstico: corre el search real (con todos los fallbacks) y devuelve los UIDs encontrados."""
    import os as _os
    _cc_sender = _os.environ.get("GMAIL_SENDER", "Cartola@itau.cl")
    _self_sender = _os.environ.get("GMAIL_USER", "")
    subject = _os.environ.get("GMAIL_SUBJECT", "Cartola Cuenta")
    try:
        poller = GmailPoller(
            subject_filter=subject,
            sender_filter=[s for s in (_cc_sender, _self_sender) if s],
        )
        with poller._connect() as conn:
            uids = poller._search_unprocessed(conn)
        return {
            "subject_filter": subject,
            "senders": poller.sender_filters,
            "uids_found": len(uids),
            "uids": [u.decode() if isinstance(u, bytes) else u for u in uids],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.api_route("/health", methods=["GET", "HEAD"])
def health(db: Session = Depends(_get_db)):
    """Healthcheck — soporta GET y HEAD (UptimeRobot free tier usa HEAD).
    Toca la DB para que el wake-up del servicio caliente también el pool de Postgres.
    """
    try:
        db.execute(select(func.count()).select_from(Categoria)).scalar()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok", "db": db_ok, "timestamp": datetime.utcnow().isoformat()}


@router.post("/sync")
def sync_from_email(db: Session = Depends(_get_db)):
    """
    Lee Gmail, descarga PDFs de cartolas CC y TC nuevos y los parsea.
    Requiere env vars: GMAIL_USER, GMAIL_APP_PASS, PDF_RUT.
    """
    import os
    rut = os.environ.get("PDF_RUT")
    if not rut:
        raise HTTPException(status_code=500, detail="PDF_RUT no configurado")

    procesados = []

    # Senders Itaú: CC y TC vienen de remitentes distintos
    import os as _os
    _cc_sender = _os.environ.get("GMAIL_SENDER", "Cartola@itau.cl")
    _tc_sender = _os.environ.get("GMAIL_SENDER_TC", "itau@eeccvirtual.cl")
    _self_sender = _os.environ.get("GMAIL_USER", "")

    # ── Cartola CC (Estado de Cuenta Personal Itaú) ───────────────────────────
    try:
        cc_poller = GmailPoller(
            subject_filter=_os.environ.get("GMAIL_SUBJECT", "Cartola Cuenta"),
            sender_filter=[s for s in (_cc_sender, _self_sender) if s],
        )
        cc_raws = cc_poller.fetch_new()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error conectando a Gmail: {exc}")

    cc_parser = CartolaITAUCCParser(password=rut)
    for raw in cc_raws:
        try:
            cartola = cc_parser.parse(raw.pdf_bytes)
            cartola_id = _save_cartola(db, cartola.to_dict(), raw.uid)
            cc_poller.mark_processed(raw.uid)
            procesados.append({
                "uid": raw.uid,
                "tipo": "cc",
                "periodo": cartola.periodo,
                "movimientos": len(cartola.movimientos),
                "cartola_id": cartola_id,
            })
            logger.info("Cartola CC procesada: %s (%d mov)", cartola.periodo, len(cartola.movimientos))
        except Exception as exc:
            logger.error("Error procesando cartola CC UID %s: %s", raw.uid, exc)
            procesados.append({"uid": raw.uid, "tipo": "cc", "error": str(exc)})

    # ── Estado TC (Tarjeta de Crédito Itaú) ───────────────────────────────────
    try:
        tc_poller = GmailPoller(
            subject_filter=_os.environ.get("GMAIL_SUBJECT_TC", "Estado de cuenta tarjeta de credito Mastercard Itau"),
            sender_filter=[s for s in (_tc_sender, _self_sender) if s],
        )
        tc_raws = tc_poller.fetch_new()
    except Exception as exc:
        logger.error("Error buscando emails TC: %s", exc)
        tc_raws = []

    tc_parser = CartolaITAUTCParser(password=rut)
    for raw in tc_raws:
        try:
            cartola = tc_parser.parse(raw.pdf_bytes)
            cartola_id = _save_cartola(db, cartola.to_dict(), raw.uid)
            tc_poller.mark_processed(raw.uid)
            procesados.append({
                "uid": raw.uid,
                "tipo": "tc",
                "periodo": cartola.periodo,
                "movimientos": len(cartola.movimientos),
                "cartola_id": cartola_id,
            })
            logger.info("Estado TC procesado: %s (%d mov)", cartola.periodo, len(cartola.movimientos))
        except Exception as exc:
            logger.error("Error procesando estado TC UID %s: %s", raw.uid, exc)
            procesados.append({"uid": raw.uid, "tipo": "tc", "error": str(exc)})

    # Auto-merge apple-pay con cargos TC duplicados de las cartolas recién importadas
    merged = []
    try:
        merged = _merge_apple_pay_with_tc(db)
    except Exception as exc:
        logger.error("Error en auto-merge apple-pay/TC: %s", exc)

    # Auto-dedupe: si el usuario re-envió emails, los movimientos se duplican (mismo
    # contenido pero distinto email_uid). Borramos duplicados exactos manteniendo id menor.
    auto_deleted = 0
    try:
        # Solo últimos 180 días para no escanear toda la DB cada sync
        cutoff = date.today() - timedelta(days=180)
        movs_recent = db.execute(
            select(MovimientoCC)
            .where(MovimientoCC.fecha >= cutoff)
            .order_by(MovimientoCC.id)
        ).scalars().all()
        seen: dict = {}
        for m in movs_recent:
            key = (m.fecha.isoformat(), m.descripcion, str(m.monto), m.cuenta)
            if key in seen:
                db.delete(m)
                auto_deleted += 1
            else:
                seen[key] = m.id
        if auto_deleted:
            db.commit()
    except Exception as exc:
        logger.error("Error en auto-dedupe post-sync: %s", exc)

    return {
        "procesados": procesados,
        "total": len(procesados),
        "apple_pay_merged": len(merged),
        "merged_details": merged[:20],
        "auto_dedupe_removed": auto_deleted,
    }


class ImportMovimientosBody(BaseModel):
    periodo: str
    desde: str | None = None
    hasta: str | None = None
    cuenta: str = "tarjeta-credito"
    titular: str = "Desconocido"
    movimientos: list[dict]


@router.post("/import-movements")
def import_movements(body: ImportMovimientosBody, db: Session = Depends(_get_db)):
    """Importa movimientos pre-parseados (sin PDF). Evita timeout en servidores con poca RAM."""
    uid = f"import_{body.cuenta}_{body.periodo}_{datetime.utcnow().isoformat()}"[:64]
    cartola_id = _save_cartola(db, body.model_dump(), uid)
    return {
        "cartola_id": cartola_id,
        "periodo": body.periodo,
        "cuenta": body.cuenta,
        "total_movimientos": len(body.movimientos),
    }


@router.post("/upload-pdf")
async def upload_pdf(
    pdf: UploadFile = File(...),
    rut: str = Query(..., description="RUT sin guion ni DV, ej: 19322966"),
    tipo: str = Query("cc", description="'cc' cuenta corriente, 'tc' tarjeta de crédito"),
    db: Session = Depends(_get_db),
):
    """Carga manual de un PDF de cartola o estado TC (útil para testing y carga histórica)."""
    pdf_bytes = await pdf.read()
    parser = CartolaTCParser(rut=rut) if tipo == "tc" else CartolaCCParser(rut=rut)
    try:
        cartola = parser.parse(pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Error parseando PDF: {exc}")

    uid = f"manual_{tipo}_{pdf.filename}_{datetime.utcnow().isoformat()}"
    cartola_id = _save_cartola(db, cartola.to_dict(), uid)

    return {
        "cartola_id": cartola_id,
        "tipo": tipo,
        "periodo": cartola.periodo,
        "cuenta": cartola.cuenta,
        "titular": cartola.titular,
        "saldo_inicial": cartola.saldo_inicial,
        "saldo_final": cartola.saldo_final,
        "total_movimientos": len(cartola.movimientos),
        "total_cargos": str(sum(m.cargo for m in cartola.movimientos if m.cargo)),
        "total_abonos": str(sum(m.abono for m in cartola.movimientos if m.abono)),
    }


@router.get("/movements")
def list_movements(
    desde: str | None = Query(None, description="Fecha desde YYYY-MM-DD"),
    hasta: str | None = Query(None, description="Fecha hasta YYYY-MM-DD"),
    tipo: str | None = Query(None, description="'cargo' o 'abono'"),
    cuenta: str | None = Query(None, description="'cc' cuenta corriente, 'tc' tarjeta"),
    buscar: str | None = Query(None, description="Texto en descripcion"),
    categoria_id: int | None = Query(None, description="ID de categoría (0 = sin categoría)"),
    limite: int = Query(200, le=1000),
    offset: int = Query(0),
    db: Session = Depends(_get_db),
):
    """Lista movimientos con filtros opcionales."""
    q = select(MovimientoCC).order_by(MovimientoCC.fecha.desc(), MovimientoCC.id.desc())

    filters = []
    if desde:
        filters.append(MovimientoCC.fecha >= date.fromisoformat(desde))
    if hasta:
        filters.append(MovimientoCC.fecha <= date.fromisoformat(hasta))
    if tipo == "cargo":
        filters.append(MovimientoCC.cargo != None)
    elif tipo == "abono":
        filters.append(MovimientoCC.abono != None)
    if cuenta == "cc":
        filters.append(~MovimientoCC.cuenta.in_(['apple-pay', 'tarjeta-credito']))
    elif cuenta == "tc":
        filters.append(MovimientoCC.cuenta.in_(['apple-pay', 'tarjeta-credito']))
    if buscar:
        filters.append(MovimientoCC.descripcion.ilike(f"%{buscar}%"))
    if categoria_id is not None:
        if categoria_id == 0:
            filters.append(MovimientoCC.categoria_id == None)
        else:
            filters.append(MovimientoCC.categoria_id == categoria_id)

    if filters:
        q = q.where(and_(*filters))

    total = db.execute(
        select(func.count()).select_from(q.subquery())
    ).scalar_one()

    movs = db.execute(q.limit(limite).offset(offset)).scalars().all()

    cats = db.execute(select(Categoria)).scalars().all()
    cat_map = {c.id: c for c in cats}

    return {
        "total": total,
        "offset": offset,
        "data": [
            {
                "id": m.id,
                "fecha": m.fecha.isoformat(),
                "descripcion": m.descripcion,
                "tipo": "abono" if m.abono else "cargo",
                "monto": str(m.monto),
                "cargo": str(m.cargo) if m.cargo else None,
                "abono": str(m.abono) if m.abono else None,
                "saldo": str(m.saldo) if m.saldo else None,
                "sucursal": m.sucursal,
                "numero_doc": m.numero_doc,
                "cuenta": m.cuenta,
                "cartola_id": m.cartola_id,
                "categoria_id": m.categoria_id,
                "categoria": {
                    "nombre": cat_map[m.categoria_id].nombre,
                    "icono": cat_map[m.categoria_id].icono,
                    "color": cat_map[m.categoria_id].color,
                    "es_gasto": cat_map[m.categoria_id].es_gasto,
                } if m.categoria_id and m.categoria_id in cat_map else None,
            }
            for m in movs
        ],
    }


@router.get("/summary")
def summary(
    anio: int = Query(default=date.today().year),
    mes: int = Query(default=date.today().month),
    db: Session = Depends(_get_db),
):
    """Resumen mensual: totales y top gastos."""
    desde = date(anio, mes, 1)
    # último día del mes
    if mes == 12:
        hasta = date(anio + 1, 1, 1)
    else:
        hasta = date(anio, mes + 1, 1)

    movs = db.execute(
        select(MovimientoCC)
        .where(MovimientoCC.fecha >= desde, MovimientoCC.fecha < hasta)
        .order_by(MovimientoCC.fecha)
    ).scalars().all()

    non_gasto_ids = set(
        db.execute(select(Categoria.id).where(Categoria.es_gasto == False)).scalars().all()
    )

    # Cargos que SÍ cuentan como gasto (sin categoría cuenta como gasto por defecto)
    cargos_gasto    = [m for m in movs if m.cargo and m.categoria_id not in non_gasto_ids]
    cargos_excluidos = [m for m in movs if m.cargo and m.categoria_id in non_gasto_ids]
    # Abonos que reducen el gasto (reembolsos, devoluciones — excluye no-gasto)
    abonos_gasto    = [m for m in movs if m.abono and m.categoria_id not in non_gasto_ids]

    total_cargos  = sum(m.cargo for m in cargos_gasto)  or Decimal("0")
    total_excluido = sum(m.cargo for m in cargos_excluidos) or Decimal("0")
    total_abonos  = sum(m.abono for m in abonos_gasto)  or Decimal("0")
    gasto_neto    = total_cargos - total_abonos

    top_cargos = sorted(cargos_gasto, key=lambda m: m.cargo, reverse=True)[:10]

    # Breakdown por categoría (neto = cargos - abonos de esa categoría)
    cats_all = {c.id: c for c in db.execute(select(Categoria)).scalars().all()}
    cat_totals: dict = {}

    def _init_cat(key):
        cat = cats_all.get(key) if key else None
        cat_totals[key] = {
            "categoria_id": key,
            "nombre": cat.nombre if cat else "Sin categoría",
            "icono":  cat.icono  if cat else "❓",
            "mob_id": cat.mob_id if cat else None,
            "color":  cat.color  if cat else "#888888",
            "total":  Decimal("0"),
            "count":  0,
        }

    for m in cargos_gasto:
        key = m.categoria_id
        if key not in cat_totals:
            _init_cat(key)
        cat_totals[key]["total"] += m.cargo
        cat_totals[key]["count"] += 1

    for m in abonos_gasto:
        key = m.categoria_id
        if key not in cat_totals:
            _init_cat(key)
        cat_totals[key]["total"] -= m.abono

    by_category = sorted(
        [
            {**{k: v for k, v in entry.items() if k != "total"},
             "total": str(entry["total"])}
            for entry in cat_totals.values()
            if entry["total"] > 0
        ],
        key=lambda x: int(x["total"]),
        reverse=True,
    )

    return {
        "periodo": f"{anio}-{mes:02d}",
        "total_cargos": str(total_cargos),
        "total_abonos": str(total_abonos),
        "gasto_neto": str(gasto_neto),
        "total_excluido": str(total_excluido),
        "cantidad_movimientos": len([m for m in movs if m.cargo]),
        "by_category": by_category,
        "top_10_gastos": [
            {"fecha": m.fecha.isoformat(), "descripcion": m.descripcion, "monto": str(m.cargo)}
            for m in top_cargos
        ],
    }


_MESES_ES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
             "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


@router.get("/summary/yearly")
def yearly_summary(
    anio: int = Query(default=date.today().year),
    db: Session = Depends(_get_db),
):
    """Totales de gasto mes a mes para un año (neto = cargos - abonos, excluyendo no-gasto)."""
    non_gasto_ids = set(
        db.execute(select(Categoria.id).where(Categoria.es_gasto == False)).scalars().all()
    )
    movs = db.execute(
        select(MovimientoCC)
        .where(
            MovimientoCC.fecha >= date(anio, 1, 1),
            MovimientoCC.fecha < date(anio + 1, 1, 1),
        )
    ).scalars().all()

    totales: dict[int, Decimal] = {i: Decimal("0") for i in range(1, 13)}
    for m in movs:
        if m.categoria_id not in non_gasto_ids:
            if m.cargo:
                totales[m.fecha.month] += m.cargo
            elif m.abono:
                totales[m.fecha.month] -= m.abono

    return {
        "anio": anio,
        "meses": [
            {"mes": i, "nombre": _MESES_ES[i], "total": str(max(totales[i], Decimal("0")))}
            for i in range(1, 13)
        ],
    }


@router.get("/summary/yearly/categories")
def yearly_category_breakdown(
    anio: int = Query(default=date.today().year),
    db: Session = Depends(_get_db),
):
    """Desglose por categoría y mes para gráfico de barras apiladas."""
    non_gasto_ids = set(
        db.execute(select(Categoria.id).where(Categoria.es_gasto == False)).scalars().all()
    )
    cats_all = {c.id: c for c in db.execute(select(Categoria)).scalars().all()}

    movs = db.execute(
        select(MovimientoCC)
        .where(
            MovimientoCC.fecha >= date(anio, 1, 1),
            MovimientoCC.fecha < date(anio + 1, 1, 1),
        )
    ).scalars().all()

    monthly: dict[int, dict] = {i: defaultdict(lambda: Decimal("0")) for i in range(1, 13)}
    for m in movs:
        if m.categoria_id not in non_gasto_ids:
            if m.cargo:
                monthly[m.fecha.month][m.categoria_id] += m.cargo
            elif m.abono:
                monthly[m.fecha.month][m.categoria_id] -= m.abono

    all_cat_ids: set = set()
    for month_data in monthly.values():
        all_cat_ids.update(month_data.keys())

    cats_info = {
        str(cid): {
            "nombre": cats_all[cid].nombre if cid and cid in cats_all else "Sin cat",
            "color":  cats_all[cid].color  if cid and cid in cats_all else "#888888",
            "icono":  cats_all[cid].icono  if cid and cid in cats_all else "❓",
        }
        for cid in all_cat_ids
    }

    meses_data = []
    for i in range(1, 13):
        segs = [
            {"categoria_id": cid, "total": str(total)}
            for cid, total in monthly[i].items()
            if total > 0
        ]
        segs.sort(key=lambda x: int(x["total"]), reverse=True)
        month_total = sum(monthly[i].values()) or Decimal("0")
        meses_data.append({
            "mes": i,
            "nombre": _MESES_ES[i],
            "total": str(max(month_total, Decimal("0"))),
            "categorias": segs,
        })

    return {"anio": anio, "categorias": cats_info, "meses": meses_data}
