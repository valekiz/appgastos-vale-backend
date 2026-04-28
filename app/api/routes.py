"""
Endpoints FastAPI para AppGastos.

POST /sync          — lee Gmail, procesa PDFs nuevos, guarda en DB
GET  /movements     — lista movimientos con filtros opcionales
GET  /summary       — resumen del mes (totales, top gastos)
GET  /health        — healthcheck para Railway
"""
import logging
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, and_, func

from app.models.database import (
    CartolaProcesada, MovimientoCC, get_session, init_db
)
from app.parsers.pdf_parser import CartolaCCParser
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

@router.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@router.post("/sync")
def sync_from_email(db: Session = Depends(_get_db)):
    """
    Lee Gmail, descarga PDFs de cartolas nuevos y los parsea.
    Requiere env vars: GMAIL_USER, GMAIL_APP_PASS, PDF_RUT.
    """
    import os
    rut = os.environ.get("PDF_RUT")
    if not rut:
        raise HTTPException(status_code=500, detail="PDF_RUT no configurado")

    try:
        poller = GmailPoller()
        cartolas_raw = poller.fetch_new()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error conectando a Gmail: {exc}")

    parser = CartolaCCParser(rut=rut)
    procesados = []

    for raw in cartolas_raw:
        try:
            cartola = parser.parse(raw.pdf_bytes)
            cartola_id = _save_cartola(db, cartola.to_dict(), raw.uid)
            poller.mark_processed(raw.uid)
            procesados.append({
                "uid": raw.uid,
                "periodo": cartola.periodo,
                "movimientos": len(cartola.movimientos),
                "cartola_id": cartola_id,
            })
            logger.info("Cartola procesada: %s (%d mov)", cartola.periodo, len(cartola.movimientos))
        except Exception as exc:
            logger.error("Error procesando cartola UID %s: %s", raw.uid, exc)
            procesados.append({"uid": raw.uid, "error": str(exc)})

    return {"procesados": procesados, "total": len(procesados)}


@router.post("/upload-pdf")
async def upload_pdf(
    pdf: UploadFile = File(...),
    rut: str = Query(..., description="RUT sin guion ni DV, ej: 19322966"),
    db: Session = Depends(_get_db),
):
    """Carga manual de un PDF de cartola (útil para testing y primer uso)."""
    pdf_bytes = await pdf.read()
    parser = CartolaCCParser(rut=rut)
    try:
        cartola = parser.parse(pdf_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Error parseando PDF: {exc}")

    uid = f"manual_{pdf.filename}_{datetime.utcnow().isoformat()}"
    cartola_id = _save_cartola(db, cartola.to_dict(), uid)

    return {
        "cartola_id": cartola_id,
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
    buscar: str | None = Query(None, description="Texto en descripcion"),
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
    if buscar:
        filters.append(MovimientoCC.descripcion.ilike(f"%{buscar}%"))

    if filters:
        q = q.where(and_(*filters))

    total = db.execute(
        select(func.count()).select_from(q.subquery())
    ).scalar_one()

    movs = db.execute(q.limit(limite).offset(offset)).scalars().all()

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

    total_cargos = sum(m.cargo for m in movs if m.cargo) or Decimal("0")
    total_abonos = sum(m.abono for m in movs if m.abono) or Decimal("0")
    balance = total_abonos - total_cargos

    top_cargos = sorted(
        [m for m in movs if m.cargo],
        key=lambda m: m.cargo,
        reverse=True,
    )[:10]

    return {
        "periodo": f"{anio}-{mes:02d}",
        "total_cargos": str(total_cargos),
        "total_abonos": str(total_abonos),
        "balance": str(balance),
        "cantidad_movimientos": len(movs),
        "top_10_gastos": [
            {
                "fecha": m.fecha.isoformat(),
                "descripcion": m.descripcion,
                "monto": str(m.cargo),
            }
            for m in top_cargos
        ],
    }
