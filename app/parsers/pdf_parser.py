"""
Parser para cartolas PDF de Cuenta Corriente Santander Chile.
Estrategia: extracción word-level con coordenadas X para identificar columnas.

Columnas detectadas (posición x en puntos PDF):
  FECHA       : x < 60
  SUCURSAL    : 60 ≤ x < 121
  DESCRIPCION : 121 ≤ x < 325
  N° DCTO     : 325 ≤ x < 380
  CARGOS      : 380 ≤ x < 465
  ABONOS      : 465 ≤ x < 560
  SALDO       : x ≥ 560
"""
import re
import io
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import pdfplumber
import pypdf


# ── Límites de columnas (x en puntos PDF) ────────────────────────────────────
COL_SUCURSAL_START   = 60
COL_DESC_START       = 119   # ligeramente bajo 121 para cubrir tolerancia del PDF
COL_NDCTO_START      = 325
COL_CARGO_START      = 380
COL_ABONO_START      = 465
COL_SALDO_START      = 560


# ── Estructuras de datos ──────────────────────────────────────────────────────

@dataclass
class Movimiento:
    fecha: date
    descripcion: str
    cargo: Optional[Decimal]
    abono: Optional[Decimal]
    saldo: Optional[Decimal]
    sucursal: Optional[str] = None
    numero_doc: Optional[str] = None

    @property
    def monto(self) -> Decimal:
        if self.abono:
            return self.abono
        if self.cargo:
            return -self.cargo
        return Decimal("0")

    def to_dict(self) -> dict:
        return {
            "fecha": self.fecha.isoformat(),
            "descripcion": self.descripcion,
            "cargo": str(self.cargo) if self.cargo else None,
            "abono": str(self.abono) if self.abono else None,
            "saldo": str(self.saldo) if self.saldo else None,
            "monto": str(self.monto),
            "sucursal": self.sucursal,
            "numero_doc": self.numero_doc,
        }


@dataclass
class CartolaMensual:
    periodo: str
    desde: Optional[date]
    hasta: Optional[date]
    cuenta: str
    titular: str
    movimientos: list[Movimiento] = field(default_factory=list)
    saldo_inicial: Optional[Decimal] = None
    saldo_final: Optional[Decimal] = None

    def to_dict(self) -> dict:
        return {
            "periodo": self.periodo,
            "desde": self.desde.isoformat() if self.desde else None,
            "hasta": self.hasta.isoformat() if self.hasta else None,
            "cuenta": self.cuenta,
            "titular": self.titular,
            "saldo_inicial": str(self.saldo_inicial) if self.saldo_inicial else None,
            "saldo_final": str(self.saldo_final) if self.saldo_final else None,
            "total_cargos": str(sum(m.cargo for m in self.movimientos if m.cargo)),
            "total_abonos": str(sum(m.abono for m in self.movimientos if m.abono)),
            "movimientos": [m.to_dict() for m in self.movimientos],
        }


# ── Utilidades ────────────────────────────────────────────────────────────────

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}


def _join_col_words(words: list[dict]) -> str:
    """
    Une palabras de una columna en orden x, concatenando sin espacios.
    Sirve para reconstruir números espaciados: ['2','0','.','8','6','.','333'] → '20.86.333'
    """
    sorted_w = sorted(words, key=lambda w: w['x0'])
    return ''.join(w['text'] for w in sorted_w)


def _parse_monto(texto: str) -> Optional[Decimal]:
    """
    '20.086.333' o '27.753' o '0' → Decimal.
    Formato chileno: puntos como separador de miles, sin centavos.
    """
    if not texto:
        return None
    limpio = texto.strip().replace('.', '').replace(',', '').replace('$', '').replace(' ', '')
    if not limpio or limpio == '-':
        return None
    try:
        v = Decimal(limpio)
        return v if v > 0 else None
    except InvalidOperation:
        return None


def _parse_fecha_ddmm(texto: str, anio: int) -> Optional[date]:
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})$', texto.strip())
    if m:
        try:
            return date(anio, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _parse_fecha_full(texto: str) -> Optional[date]:
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$', texto.strip())
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


# ── PDF decrypt ───────────────────────────────────────────────────────────────

def _decrypt_pdf(pdf_bytes: bytes, rut: str) -> bytes:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        result = reader.decrypt(rut)
        if result == pypdf.PasswordType.NOT_DECRYPTED:
            raise ValueError(f"No se pudo desencriptar el PDF con RUT '{rut}'")
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── Parser principal ──────────────────────────────────────────────────────────

class CartolaCCParser:
    """
    Parsea cartolas de Cuenta Corriente Santander Chile.

    Uso:
        parser = CartolaCCParser(rut="19322966")
        cartola = parser.parse(pdf_bytes)
        cartola = parser.parse_file("/path/to/file.pdf")
    """

    def __init__(self, rut: str):
        self.rut = re.sub(r'[^0-9]', '', rut)

    def parse(self, pdf_bytes: bytes) -> CartolaMensual:
        decrypted = _decrypt_pdf(pdf_bytes, self.rut)
        return self._extract(decrypted)

    def parse_file(self, path: str) -> CartolaMensual:
        with open(path, 'rb') as f:
            return self.parse(f.read())

    # ── extracción principal ──────────────────────────────────────────────────

    def _extract(self, pdf_bytes: bytes) -> CartolaMensual:
        all_lines: list[str] = []
        all_rows: list[list[dict]] = []   # cada row = lista de word-dicts de una línea

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                # Texto plano para metadata
                text = page.extract_text() or ''
                all_lines.extend(text.split('\n'))

                # Words con coordenadas para movimientos
                words = page.extract_words(x_tolerance=3, y_tolerance=3)
                rows = self._group_by_row(words)
                all_rows.extend(rows)

        desde, hasta, anio = self._extract_periodo(all_lines)
        cuenta = self._extract_cuenta(all_lines)
        titular = self._extract_titular(all_lines)
        saldo_inicial, saldo_final = self._extract_saldos(all_lines)
        movimientos = self._parse_movimientos(all_rows, anio)
        movimientos = self._dedup(movimientos)

        if hasta:
            mes_nombre = [k for k, v in _MESES.items() if v == hasta.month]
            periodo = f"{mes_nombre[0].capitalize()} {hasta.year}" if mes_nombre else str(hasta)
        else:
            periodo = "Desconocido"

        return CartolaMensual(
            periodo=periodo,
            desde=desde,
            hasta=hasta,
            cuenta=cuenta,
            titular=titular,
            movimientos=movimientos,
            saldo_inicial=saldo_inicial,
            saldo_final=saldo_final,
        )

    def _group_by_row(self, words: list[dict]) -> list[list[dict]]:
        """Agrupa palabras por posición Y (tolerancia 5pt) → filas."""
        rows: dict[int, list[dict]] = {}
        for w in words:
            y_key = round(w['top'] / 5) * 5
            rows.setdefault(y_key, []).append(w)
        return [sorted(row, key=lambda w: w['x0']) for row in rows.values()]

    # ── metadata (texto plano) ────────────────────────────────────────────────

    def _extract_periodo(self, lines: list[str]) -> tuple[Optional[date], Optional[date], int]:
        for i, line in enumerate(lines):
            if 'CARTOLA DESDE' in line.upper():
                for j in range(i, min(i + 4, len(lines))):
                    dates = re.findall(r'\d{2}/\d{2}/\d{4}', lines[j])
                    if len(dates) >= 2:
                        desde = _parse_fecha_full(dates[0])
                        hasta = _parse_fecha_full(dates[1])
                        anio = hasta.year if hasta else date.today().year
                        return desde, hasta, anio
        # Fallback
        for line in lines:
            dates = re.findall(r'\d{2}/\d{2}/\d{4}', line)
            if len(dates) >= 2:
                desde = _parse_fecha_full(dates[0])
                hasta = _parse_fecha_full(dates[1])
                if hasta and 2000 <= hasta.year <= 2100:
                    return desde, hasta, hasta.year
        return None, None, date.today().year

    def _extract_cuenta(self, lines: list[str]) -> str:
        for line in lines:
            m = re.search(r'\b(\d-\d{3}-\d{2}-\d{5}-\d)\b', line)
            if m:
                return m.group(1)
        return 'Desconocida'

    def _extract_titular(self, lines: list[str]) -> str:
        for i, line in enumerate(lines):
            if re.search(r'\d-\d{3}-\d{2}-\d{5}-\d', line):
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if re.match(r'^[A-ZÁÉÍÓÚÑ\s]{10,}$', next_line):
                        return next_line.title()
        return 'Desconocido'

    def _extract_saldos(self, lines: list[str]) -> tuple[Optional[Decimal], Optional[Decimal]]:
        for i, line in enumerate(lines):
            if 'SALDO INICIAL' in line.upper() and 'SALDO FINAL' in line.upper():
                for j in range(i + 1, min(i + 4, len(lines))):
                    numeros = re.findall(r'[\d\.]+', lines[j].strip())
                    if len(numeros) >= 2:
                        return _parse_monto(numeros[0]), _parse_monto(numeros[-1])
        return None, None

    # ── movimientos (coordenadas) ─────────────────────────────────────────────

    def _dedup(self, movimientos: list[Movimiento]) -> list[Movimiento]:
        """Elimina duplicados exactos (fecha + descripcion + monto). El PDF repite
        comisiones en la sección 'Resumen de Comisiones' al final."""
        seen: set[tuple] = set()
        result = []
        for m in movimientos:
            key = (m.fecha, m.descripcion, m.cargo, m.abono)
            if key not in seen:
                seen.add(key)
                result.append(m)
        return result

    def _parse_movimientos(self, rows: list[list[dict]], anio: int) -> list[Movimiento]:
        movimientos = []
        for row in rows:
            if not row:
                continue
            first = row[0]['text']
            if not re.match(r'^\d{2}/\d{2}$', first):
                continue
            mov = self._parse_row(row, anio)
            if mov:
                movimientos.append(mov)
        return movimientos

    def _parse_row(self, row: list[dict], anio: int) -> Optional[Movimiento]:
        """
        Clasifica cada palabra de la fila en su columna según x0,
        reconstruye los valores y crea un Movimiento.
        """
        fecha_words: list[dict] = []
        suc_words:   list[dict] = []
        desc_words:  list[dict] = []
        ndcto_words: list[dict] = []
        cargo_words: list[dict] = []
        abono_words: list[dict] = []
        saldo_words: list[dict] = []

        for w in row:
            x = w['x0']
            if x < COL_SUCURSAL_START:
                fecha_words.append(w)
            elif x < COL_DESC_START:
                suc_words.append(w)
            elif x < COL_NDCTO_START:
                desc_words.append(w)
            elif x < COL_CARGO_START:
                ndcto_words.append(w)
            elif x < COL_ABONO_START:
                cargo_words.append(w)
            elif x < COL_SALDO_START:
                abono_words.append(w)
            else:
                saldo_words.append(w)

        fecha_str = _join_col_words(fecha_words)
        fecha = _parse_fecha_ddmm(fecha_str, anio)
        if not fecha:
            return None

        descripcion = ' '.join(w['text'] for w in sorted(desc_words, key=lambda w: w['x0']))
        sucursal    = ' '.join(w['text'] for w in sorted(suc_words, key=lambda w: w['x0'])) or None
        numero_doc  = _join_col_words(ndcto_words) if ndcto_words else None

        cargo_str = _join_col_words(cargo_words) if cargo_words else None
        abono_str = _join_col_words(abono_words) if abono_words else None
        saldo_str = _join_col_words(saldo_words) if saldo_words else None

        cargo = _parse_monto(cargo_str)
        abono = _parse_monto(abono_str)
        saldo = _parse_monto(saldo_str)

        if not descripcion.strip():
            return None

        return Movimiento(
            fecha=fecha,
            descripcion=descripcion,
            cargo=cargo,
            abono=abono,
            saldo=saldo,
            sucursal=sucursal,
            numero_doc=numero_doc if numero_doc and numero_doc.strip() else None,
        )


# ── TC Parser ─────────────────────────────────────────────────────────────────

_TC_DATE_RE   = re.compile(r'\b(\d{2}/\d{2}/\d{4})\b')
_TC_AMOUNT_RE = re.compile(r'\$\s*-?([\d.]+)\s*$')
_TC_PERIODO_RE = re.compile(r'PERIODO\s+FACTURADO', re.IGNORECASE)
_TC_STUB_RE   = re.compile(r'CUP[OÓ]N\s+DE\s+PAGO', re.IGNORECASE)

# Lines that start with these are totals/summaries, not transactions
_TC_SUMMARY_STARTS = (
    'TOTAL ', 'SALDO ', 'MONTO M', 'SUBTOTAL',
)

# Any of these anywhere in the combined text → treat as abono
_ABONO_KEYWORDS = ('MONTO CANCELADO', 'NOTA DE CREDITO', 'NOTA DE CRÉDITO', 'NOTA CRÉDITO')


class CartolaTCParser:
    """
    Parsea estados de cuenta de Tarjeta de Crédito Santander Chile.

    Formato de línea de transacción: LUGAR DD/MM/YYYY DESCRIPCION $ MONTO

    Uso:
        parser = CartolaTCParser(rut="19322966")
        cartola = parser.parse(pdf_bytes)
        cartola = parser.parse_file("/path/to/file.pdf")
    """

    def __init__(self, rut: str):
        self.rut = re.sub(r'[^0-9]', '', rut)

    def parse(self, pdf_bytes: bytes) -> CartolaMensual:
        decrypted = _decrypt_pdf(pdf_bytes, self.rut)
        return self._extract(decrypted)

    def parse_file(self, path: str) -> CartolaMensual:
        with open(path, 'rb') as f:
            return self.parse(f.read())

    # ── extracción principal ──────────────────────────────────────────────────

    def _extract(self, pdf_bytes: bytes) -> CartolaMensual:
        all_lines: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ''
                all_lines.extend(text.split('\n'))

        desde, hasta = self._extract_periodo(all_lines)
        titular = self._extract_titular(all_lines)
        movimientos = self._parse_movimientos(all_lines)
        movimientos = self._dedup(movimientos)

        if hasta:
            mes_nombre = [k for k, v in _MESES.items() if v == hasta.month]
            periodo = f"{mes_nombre[0].capitalize()} {hasta.year}" if mes_nombre else str(hasta)
        else:
            periodo = "Desconocido"

        return CartolaMensual(
            periodo=periodo,
            desde=desde,
            hasta=hasta,
            cuenta='tarjeta-credito',
            titular=titular,
            movimientos=movimientos,
        )

    # ── metadata ──────────────────────────────────────────────────────────────

    def _extract_periodo(self, lines: list[str]) -> tuple[Optional[date], Optional[date]]:
        for line in lines:
            if _TC_PERIODO_RE.search(line):
                dates = re.findall(r'\d{2}/\d{2}/\d{4}', line)
                if len(dates) >= 2:
                    return _parse_fecha_full(dates[0]), _parse_fecha_full(dates[1])
        # Fallback: first line with two adjacent dates
        for line in lines:
            dates = re.findall(r'\d{2}/\d{2}/\d{4}', line)
            if len(dates) >= 2:
                d1, d2 = _parse_fecha_full(dates[0]), _parse_fecha_full(dates[1])
                if d1 and d2 and 2000 <= d2.year <= 2100:
                    return d1, d2
        return None, None

    def _extract_titular(self, lines: list[str]) -> str:
        for i, line in enumerate(lines):
            if 'TITULAR' in line.upper():
                m = re.search(r'TITULAR[:\s]+([A-ZÁÉÍÓÚÑ\s]{5,})', line, re.IGNORECASE)
                if m:
                    return m.group(1).strip().title()
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    if re.match(r'^[A-ZÁÉÍÓÚÑ\s]{5,}$', nxt):
                        return nxt.title()
        return 'Desconocido'

    # ── movimientos ────────────────────────────────────────────────────────────

    def _parse_movimientos(self, lines: list[str]) -> list[Movimiento]:
        movimientos: list[Movimiento] = []
        in_stub = False
        for line in lines:
            s = line.strip()
            if not s:
                continue
            # Detect and skip payment stub section
            if _TC_STUB_RE.search(s):
                in_stub = True
            if in_stub:
                continue
            # Skip period header (contains dates but is not a transaction)
            if _TC_PERIODO_RE.search(s):
                continue
            # Skip lines with no date — headers, totals, etc.
            if not _TC_DATE_RE.search(s):
                continue
            # Skip summary/total lines that happen to have a date
            upper = s.upper()
            if any(upper.startswith(pat) for pat in _TC_SUMMARY_STARTS):
                continue
            mov = self._parse_line(s)
            if mov:
                movimientos.append(mov)
        return movimientos

    def _parse_line(self, line: str) -> Optional[Movimiento]:
        date_m = _TC_DATE_RE.search(line)
        if not date_m:
            return None
        fecha = _parse_fecha_full(date_m.group(1))
        if not fecha:
            return None

        before = line[:date_m.start()].strip()   # LUGAR
        after  = line[date_m.end():].strip()       # DESCRIPCION $ MONTO

        # Amount must be at the end: $ MONTO
        amount_m = _TC_AMOUNT_RE.search(after)
        if not amount_m:
            return None

        cargo_val = _parse_monto(amount_m.group(1))
        if not cargo_val:
            return None

        desc_part = after[:amount_m.start()].strip()
        full_text = f"{before} {desc_part}".strip() if before else desc_part
        if not full_text or full_text.startswith('$'):
            return None

        is_abono = any(kw in full_text.upper() for kw in _ABONO_KEYWORDS)

        return Movimiento(
            fecha=fecha,
            descripcion=full_text,
            cargo=None if is_abono else cargo_val,
            abono=cargo_val if is_abono else None,
            saldo=None,
        )

    def _dedup(self, movimientos: list[Movimiento]) -> list[Movimiento]:
        seen: set[tuple] = set()
        result = []
        for m in movimientos:
            key = (m.fecha, m.descripcion.upper(), m.cargo, m.abono)
            if key not in seen:
                seen.add(key)
                result.append(m)
        return result
