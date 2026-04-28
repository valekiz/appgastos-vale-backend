"""
Modelos SQLAlchemy y setup de base de datos SQLite.
"""
import os
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean, Column, Date, Integer, Numeric, String, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Session

DB_PATH = os.environ.get("DB_PATH", "gastos.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class Base(DeclarativeBase):
    pass


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
    procesado_en   = Column(String(30))   # ISO datetime


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
    monto        = Column(Numeric(18, 0))   # abono positivo, cargo negativo
    sucursal     = Column(String(50))
    numero_doc   = Column(String(50))
    cuenta       = Column(String(30))       # número de cuenta para multi-cuenta futuro


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return Session(engine)
