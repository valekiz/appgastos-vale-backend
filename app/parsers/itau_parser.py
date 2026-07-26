"""
Parser para PDFs de Itaú Chile.
- Estado de Cuenta Personal (cuenta corriente)
- Estado de Cuenta Nacional de Tarjeta de Crédito
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

import pdfplumber


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_monto(s: str) -> Optional[Decimal]:
    """'1.234.567' o '$1.234' → Decimal positiva."""
    s = s.strip().lstrip('$').replace('.', '').replace(',', '').replace(' ', '')
    if not s or s in ('-', ''):
        return None
    try:
        v = Decimal(s)
        return abs(v) if v != 0 else None
    except InvalidOperation:
        return None


def _parse_date(s: str) -> Optional[date]:
    for fmt in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


# ── Estructuras de datos ──────────────────────────────────────────────────────

@dataclass
class MovimientoItau:
    fecha: date
    descripcion: str
    cargo: Optional[Decimal]
    abono: Optional[Decimal]
    saldo: Optional[Decimal] = None
    sucursal: Optional[str] = None
    numero_doc: Optional[str] = None

    @property
    def monto(self) -> Decimal:
        if self.abono:
            return self.abono
        if self.cargo:
            return -self.cargo
        return Decimal('0')

    def to_dict(self) -> dict:
        return {
            'fecha': self.fecha.isoformat(),
            'descripcion': self.descripcion,
            'cargo': str(self.cargo) if self.cargo else None,
            'abono': str(self.abono) if self.abono else None,
            'saldo': str(self.saldo) if self.saldo else None,
            'monto': str(self.monto),
            'sucursal': self.sucursal,
            'numero_doc': self.numero_doc,
        }


@dataclass
class CartolaParsedItau:
    periodo: str
    desde: Optional[date]
    hasta: Optional[date]
    cuenta: str
    titular: str
    movimientos: list[MovimientoItau] = field(default_factory=list)
    saldo_inicial: Optional[Decimal] = None
    saldo_final: Optional[Decimal] = None

    def to_dict(self) -> dict:
        return {
            'periodo': self.periodo,
            'desde': self.desde.isoformat() if self.desde else None,
            'hasta': self.hasta.isoformat() if self.hasta else None,
            'cuenta': self.cuenta,
            'titular': self.titular,
            'saldo_inicial': str(self.saldo_inicial) if self.saldo_inicial else None,
            'saldo_final': str(self.saldo_final) if self.saldo_final else None,
            'total_cargos': str(sum(m.cargo for m in self.movimientos if m.cargo)),
            'total_abonos': str(sum(m.abono for m in self.movimientos if m.abono)),
            'movimientos': [m.to_dict() for m in self.movimientos],
        }


# ── CC Parser ─────────────────────────────────────────────────────────────────

# Códigos de operación Itaú CC: 'cargo', 'abono', o None (saltar)
_CC_CODIGO_MAP = {
    '042': 'cargo',   # compra con débito
    '099': 'cargo',   # transferencia saliente
    '806': 'cargo',   # pago tarjeta de crédito
    '820': 'cargo',   # cuota préstamo
    '882': 'cargo',   # intereses / comisión
    '213': 'cargo',   # impuesto
    '169': 'cargo',   # transferencia propia cuenta saliente
    '100': 'abono',   # transferencia entrante
    '795': 'abono',   # pago proveedores recibido
    '019': 'abono',   # devolución de mercadería
    '170': 'abono',   # transferencia propia cuenta entrante
    '611': None,      # abono desde línea de crédito (mecanismo LC, saltar)
    '610': None,      # cargo LC por falta de fondos (saltar)
    '612': None,      # cargo ctacte por traspaso LC (saltar)
    '613': None,      # pago automático línea de crédito (saltar)
    '160': None,      # transferencia propia desde LC (saltar)
}

_AMOUNT_RE = re.compile(r'^-?[\d.]+$')
_OP_NUM_RE = re.compile(r'^\d{6,12}$')
_CODIGO_RE = re.compile(r'^\d{3}$')
_SUCURSAL_RE = re.compile(r'^\d{3,4}$')
_DATE4_RE = re.compile(r'^\d{2}/\d{2}/\d{4}$')

_CC_PERIOD_RE = re.compile(r'Período\s*:\s*(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})')
_CC_SALDO_FINAL_RE = re.compile(r'Saldo Final\s+([\d.]+)')


class CartolaITAUCCParser:
    """Parser para Estado de Cuenta Personal de Itaú Chile."""

    def __init__(self, password: str = ''):
        self.password = password

    def parse(self, pdf_bytes: bytes) -> CartolaParsedItau:
        kwargs = {'password': self.password} if self.password else {}
        with pdfplumber.open(io.BytesIO(pdf_bytes), **kwargs) as pdf:
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
        full_text = '\n'.join(pages_text)
        return self._parse_text(full_text)

    def _parse_text(self, text: str) -> CartolaParsedItau:
        lines = text.split('\n')

        titular = self._extract_titular(lines)
        desde, hasta = self._extract_period(text)
        saldo_final = self._extract_saldo_final(text)
        periodo = (f"{desde.strftime('%d/%m/%Y')} - {hasta.strftime('%d/%m/%Y')}"
                   if desde and hasta else '')
        movimientos = self._parse_movimientos(lines)

        return CartolaParsedItau(
            periodo=periodo,
            desde=desde,
            hasta=hasta,
            cuenta='cuenta-corriente',
            titular=titular,
            movimientos=movimientos,
            saldo_final=saldo_final,
        )

    def _extract_titular(self, lines: list[str]) -> str:
        skip = {'ESTADO', 'MOVIMIENTOS', 'VISVIRI', 'CONDES', 'SANTIAGO',
                'CUENTA', 'PERSONAL', 'LINEA', 'CREDITO', 'SUCURSAL'}
        for line in lines[:15]:
            line = line.strip()
            if (line.isupper() and len(line) > 10
                    and not any(w in line for w in skip)
                    and re.match(r'^[A-ZÁÉÍÓÚÑ\s]+$', line)):
                return line.title()
        return 'Desconocido'

    def _extract_period(self, text: str) -> tuple[Optional[date], Optional[date]]:
        m = _CC_PERIOD_RE.search(text)
        if m:
            return _parse_date(m.group(1)), _parse_date(m.group(2))
        return None, None

    def _extract_saldo_final(self, text: str) -> Optional[Decimal]:
        m = _CC_SALDO_FINAL_RE.search(text)
        return _parse_monto(m.group(1)) if m else None

    def _parse_movimientos(self, lines: list[str]) -> list[MovimientoItau]:
        movs = []
        in_movimientos = False

        for line in lines:
            stripped = line.strip()

            # Activar cuando encontremos la sección CC MOVIMIENTOS
            if stripped == 'MOVIMIENTOS' or stripped.startswith('MOVIMIENTOS\n'):
                in_movimientos = True
                continue

            # Desactivar al llegar al resumen o a una sección diferente
            if in_movimientos and (
                'RESUMEN DE MOVIMIENTOS' in stripped
                or 'LIQ. DE INTERESES' in stripped
                or 'ESTADO DE LINEA' in stripped
            ):
                in_movimientos = False
                continue

            if not in_movimientos:
                continue

            mov = self._parse_line(stripped)
            if mov:
                movs.append(mov)

        return movs

    def _parse_line(self, line: str) -> Optional[MovimientoItau]:
        tokens = line.split()
        if not tokens or not _DATE4_RE.match(tokens[0]):
            return None

        idx = 0
        fecha = _parse_date(tokens[idx])
        if not fecha:
            return None
        idx += 1

        # Número de operación opcional (6–12 dígitos)
        op_num = None
        if idx < len(tokens) and _OP_NUM_RE.match(tokens[idx]):
            op_num = tokens[idx]
            idx += 1

        # Sucursal (3–4 dígitos)
        if idx >= len(tokens) or not _SUCURSAL_RE.match(tokens[idx]):
            return None
        sucursal = tokens[idx]
        idx += 1

        # Código (3 dígitos)
        if idx >= len(tokens) or not _CODIGO_RE.match(tokens[idx]):
            return None
        codigo = tokens[idx]
        idx += 1

        # Verificar si saltamos este código
        direction = _CC_CODIGO_MAP.get(codigo)
        if codigo in _CC_CODIGO_MAP and direction is None:
            return None  # movimiento de línea de crédito, ignorar

        remaining = tokens[idx:]
        if not remaining:
            return None

        # Extraer los últimos 1–2 tokens numéricos como monto y saldo
        amounts: list[str] = []
        desc_end = len(remaining)
        i = len(remaining) - 1
        while i >= 0 and i >= len(remaining) - 2:
            if _AMOUNT_RE.match(remaining[i]):
                amounts.insert(0, remaining[i])
                desc_end = i
                i -= 1
            else:
                break

        if not amounts:
            return None

        monto = _parse_monto(amounts[0])
        saldo = _parse_monto(amounts[1]) if len(amounts) > 1 else None

        if not monto:
            return None

        description = ' '.join(remaining[:desc_end]).strip()
        if not description:
            return None

        # Si el código no está en el mapa, defaultear a cargo
        if direction is None:
            direction = 'cargo'

        return MovimientoItau(
            fecha=fecha,
            descripcion=description,
            cargo=monto if direction == 'cargo' else None,
            abono=monto if direction == 'abono' else None,
            saldo=saldo,
            sucursal=sucursal,
            numero_doc=op_num,
        )


# ── TC Parser ─────────────────────────────────────────────────────────────────

_TC_PERIOD_RE = re.compile(
    r'PERÍODO FACTURADO\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})'
)
_TC_TITULAR_RE = re.compile(r'NOMBRE DEL TITULAR\s+(.+)')
_TC_DATE_RE = re.compile(r'\b(\d{2}/\d{2}/\d{2})\b')
_TC_DOLLAR_RE = re.compile(r'\$(-?[\d.]+)')


class CartolaITAUTCParser:
    """Parser para Estado de Cuenta Nacional de Tarjeta de Crédito Itaú Chile."""

    def __init__(self, password: str = ''):
        self.password = password

    def parse(self, pdf_bytes: bytes) -> CartolaParsedItau:
        kwargs = {'password': self.password} if self.password else {}
        with pdfplumber.open(io.BytesIO(pdf_bytes), **kwargs) as pdf:
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
        full_text = '\n'.join(pages_text)
        return self._parse_text(full_text)

    def _parse_text(self, text: str) -> CartolaParsedItau:
        lines = text.split('\n')

        titular = self._extract_titular(text)
        desde, hasta = self._extract_period(text)
        periodo = (f"{desde.strftime('%d/%m/%Y')} - {hasta.strftime('%d/%m/%Y')}"
                   if desde and hasta else '')
        movimientos = self._parse_movimientos(lines)

        return CartolaParsedItau(
            periodo=periodo,
            desde=desde,
            hasta=hasta,
            cuenta='tarjeta-credito',
            titular=titular,
            movimientos=movimientos,
        )

    def _extract_titular(self, text: str) -> str:
        m = _TC_TITULAR_RE.search(text)
        if m:
            return m.group(1).strip().title()
        return 'Desconocido'

    def _extract_period(self, text: str) -> tuple[Optional[date], Optional[date]]:
        m = _TC_PERIOD_RE.search(text)
        if m:
            return _parse_date(m.group(1)), _parse_date(m.group(2))
        return None, None

    def _parse_movimientos(self, lines: list[str]) -> list[MovimientoItau]:
        movs = []
        in_section = False

        for line in lines:
            stripped = line.strip()

            # Activar en sección 2 (período actual) y 3 (cargos/comisiones)
            if re.match(r'^[12]\.\s*(PERÍODO ACTUAL|TOTAL OPERACIONES|CARGOS,)', stripped):
                in_section = True
                continue
            if re.match(r'^(III\.|2\.PRODUCTOS|TOTAL TARJETA)', stripped):
                in_section = False
                continue

            if not in_section:
                continue

            mov = self._parse_line(stripped)
            if mov:
                movs.append(mov)

        return movs

    def _parse_line(self, line: str) -> Optional[MovimientoItau]:
        # Buscar fecha DD/MM/YY en la línea
        m = _TC_DATE_RE.search(line)
        if not m:
            return None

        fecha = _parse_date(m.group(1))
        if not fecha:
            return None

        # Saltar pagos (MONTO CANCELADO = abono al banco, no es gasto)
        if 'MONTO CANCELADO' in line:
            return None

        # Todos los montos con $ en la línea; el último = valor cuota mensual
        dollar_amounts = _TC_DOLLAR_RE.findall(line)
        if not dollar_amounts:
            return None

        valor_cuota = _parse_monto(dollar_amounts[-1])
        if not valor_cuota or valor_cuota <= 0:
            return None

        # Extraer descripción: parte después del código DDMM y referencia
        after_date = line[m.end():].strip()
        tokens = after_date.split()

        idx = 0
        if idx < len(tokens) and re.match(r'^\d{4}$', tokens[idx]):
            idx += 1  # saltar código de fecha DDMM
        if idx < len(tokens) and re.match(r'^\d{7,9}$', tokens[idx]):
            idx += 1  # saltar número de referencia

        desc_tokens = []
        for token in tokens[idx:]:
            if token.startswith('$') or re.match(r'^\d{2}/\d{2}$', token):
                break
            if token in ('TASA', 'INT.'):
                break
            desc_tokens.append(token)

        description = ' '.join(desc_tokens).strip()
        if not description:
            return None

        return MovimientoItau(
            fecha=fecha,
            descripcion=description,
            cargo=valor_cuota,
            abono=None,
        )
