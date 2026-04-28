"""
Prueba el parser contra el PDF real de Santander.
Uso:
    cd backend
    pip install -r requirements.txt
    python -m pytest tests/test_pdf_parser.py -v
    # o directamente:
    python tests/test_pdf_parser.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.parsers.pdf_parser import CartolaCCParser


PDF_PATH = r"C:\Users\andres.killinger\Downloads\1_15152_000097180172_24122021_CC.pdf"
RUT = "19322966"


def test_parse_cartola_real():
    parser = CartolaCCParser(rut=RUT)
    cartola = parser.parse_file(PDF_PATH)

    print("\n" + "=" * 60)
    print(f"Período   : {cartola.periodo}")
    print(f"Cuenta    : {cartola.cuenta}")
    print(f"Titular   : {cartola.titular}")
    print(f"Saldo ini : {cartola.saldo_inicial}")
    print(f"Saldo fin : {cartola.saldo_final}")
    print(f"Movimientos encontrados: {len(cartola.movimientos)}")
    print("=" * 60)

    for i, mov in enumerate(cartola.movimientos[:20], 1):
        signo = "+" if mov.abono else "-"
        monto_str = f"{signo}{mov.cargo or mov.abono}"
        print(f"  {i:02d}. {mov.fecha}  {mov.descripcion[:40]:<40}  {monto_str:>15}")

    if len(cartola.movimientos) > 20:
        print(f"  ... y {len(cartola.movimientos) - 20} movimientos más")

    assert cartola.periodo != "Desconocido", "No se detectó el período"
    assert len(cartola.movimientos) > 0, "No se encontraron movimientos"
    print("\n✓ Parser OK")


if __name__ == "__main__":
    test_parse_cartola_real()
