"""
Modelos SQLAlchemy. Soporta PostgreSQL (producción) y SQLite (desarrollo local).
"""
import logging
import os

from sqlalchemy import (
    Boolean, Column, Date, Integer, Numeric, String, Text,
    create_engine, func, select, text,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase, Session

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if "supabase.co" in DATABASE_URL and "sslmode" not in DATABASE_URL:
        DATABASE_URL += "?sslmode=require"
    engine = create_engine(DATABASE_URL)
else:
    DB_PATH = os.environ.get("DB_PATH", "gastos.db")
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


class Categoria(Base):
    """Categoría de gasto con ícono de monstruo MapleStory."""
    __tablename__ = "categorias"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    nombre     = Column(String(50), nullable=False)
    icono      = Column(String(10))       # emoji de respaldo
    mob_id     = Column(Integer)          # ID de monstruo en maplestory.io
    es_gasto   = Column(Boolean, default=True, nullable=False)
    color      = Column(String(10))       # color hex para UI
    es_sistema = Column(Boolean, default=False)  # no se puede borrar


class CartolaProcesada(Base):
    """Registro de cada PDF de cartola que ya fue procesado."""
    __tablename__ = "cartolas_procesadas"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    email_uid      = Column(String(64), unique=True, nullable=False, index=True)
    periodo        = Column(String(50))
    cuenta         = Column(String(30))
    titular        = Column(String(100))
    saldo_inicial  = Column(Numeric(18, 0))
    saldo_final    = Column(Numeric(18, 0))
    desde          = Column(Date)
    hasta          = Column(Date)
    procesado_en   = Column(String(30))


class MovimientoCC(Base):
    """Un movimiento de cuenta corriente."""
    __tablename__ = "movimientos_cc"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    cartola_id   = Column(Integer, nullable=False, index=True)
    fecha        = Column(Date, nullable=False, index=True)
    descripcion  = Column(Text, nullable=False)
    cargo        = Column(Numeric(18, 0))
    abono        = Column(Numeric(18, 0))
    saldo        = Column(Numeric(18, 0))
    monto        = Column(Numeric(18, 0))
    sucursal     = Column(String(50))
    numero_doc   = Column(String(50))
    cuenta       = Column(String(30))
    categoria_id = Column(Integer)


# ── Categorías por defecto ────────────────────────────────────────────────────
_CATEGORIAS_DEFAULT = [
    dict(nombre="Salidas",         icono="🍄", mob_id=130100,  es_gasto=True,  color="#ff6600", es_sistema=True),
    dict(nombre="Transporte",      icono="🚗", mob_id=None,    es_gasto=True,  color="#4488ff", es_sistema=True),
    dict(nombre="Entretenimiento", icono="👾", mob_id=210100,  es_gasto=True,  color="#aa44ff", es_sistema=True),
    dict(nombre="Hogar",           icono="🏠", mob_id=130101,  es_gasto=True,  color="#44aa44", es_sistema=True),
    dict(nombre="Salud",           icono="💊", mob_id=2230101, es_gasto=True,  color="#ff4488", es_sistema=True),
    dict(nombre="Vestuario",       icono="👔", mob_id=None,    es_gasto=True,  color="#ffaa44", es_sistema=True),
    dict(nombre="Educación",       icono="📚", mob_id=None,    es_gasto=True,  color="#44ddff", es_sistema=True),
    dict(nombre="Deporte",         icono="🏓", mob_id=None,    es_gasto=True,  color="#00bbff", es_sistema=True),
    dict(nombre="Banco",           icono="🏦", mob_id=None,    es_gasto=True,  color="#5599ff", es_sistema=True),
    dict(nombre="Regalos",        icono="🎁", mob_id=None,    es_gasto=True,  color="#ff66aa", es_sistema=True),
    dict(nombre="Otro",            icono="❓", mob_id=None,    es_gasto=True,  color="#888888", es_sistema=True),
    dict(nombre="Inversión",       icono="💰", mob_id=9300003, es_gasto=False, color="#ffd700", es_sistema=True),
    dict(nombre="Por Cobrar",      icono="🤝", mob_id=8820001, es_gasto=False, color="#88ff88", es_sistema=True),
    dict(nombre="No es Gasto",     icono="✖️", mob_id=8800000, es_gasto=False, color="#aaaaaa", es_sistema=True),
]

# Actualizaciones a categorías ya existentes en producción
_CATEGORIAS_UPDATES = [
    ("Alimentación", {"nombre": "Salidas", "icono": "🍄", "mob_id": 130100}),
    ("Transporte",   {"icono": "🚗", "mob_id": None}),
]

# Categorías nuevas que se agregan si no existen
_CATEGORIAS_NUEVAS = [
    dict(nombre="Deporte",  icono="🏓", mob_id=None, es_gasto=True, color="#00bbff", es_sistema=True),
    dict(nombre="Banco",    icono="🏦", mob_id=None, es_gasto=True, color="#5599ff", es_sistema=True),
    dict(nombre="Regalos",  icono="🎁", mob_id=None, es_gasto=True, color="#ff66aa", es_sistema=True),
]


def _migrate():
    """Agrega columnas nuevas a tablas existentes sin borrar datos."""
    inspector = sa_inspect(engine)
    if "movimientos_cc" in inspector.get_table_names():
        cols = [c["name"] for c in inspector.get_columns("movimientos_cc")]
        if "categoria_id" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE movimientos_cc ADD COLUMN categoria_id INTEGER"))
                conn.commit()
                logger.info("Migración: columna categoria_id añadida a movimientos_cc")


def _seed_categorias():
    """Crea o actualiza las categorías por defecto."""
    with Session(engine) as db:
        count = db.execute(select(func.count()).select_from(Categoria)).scalar_one()
        if count == 0:
            for cat_data in _CATEGORIAS_DEFAULT:
                db.add(Categoria(**cat_data))
            logger.info("Categorías por defecto creadas (%d)", len(_CATEGORIAS_DEFAULT))
        else:
            # Aplicar renombres/actualizaciones a categorías ya existentes
            for old_nombre, updates in _CATEGORIAS_UPDATES:
                cat = db.execute(
                    select(Categoria).where(Categoria.nombre == old_nombre)
                ).scalar_one_or_none()
                if cat:
                    for k, v in updates.items():
                        setattr(cat, k, v)
            # Agregar categorías nuevas que no existan
            for cat_data in _CATEGORIAS_NUEVAS:
                exists = db.execute(
                    select(Categoria).where(Categoria.nombre == cat_data["nombre"])
                ).scalar_one_or_none()
                if not exists:
                    db.add(Categoria(**cat_data))
        db.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate()
    _seed_categorias()


def get_session() -> Session:
    return Session(engine)
