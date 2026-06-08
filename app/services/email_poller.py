"""
Poller de Gmail para cartolas Santander.
Usa IMAP con contraseña de aplicación de Google (requiere 2FA activo en la cuenta).

Configuración necesaria:
  GMAIL_USER     = akillinger100@gmail.com
  GMAIL_APP_PASS = xxxx xxxx xxxx xxxx   (contraseña de aplicación Google)
  GMAIL_SUBJECT  = Cartola Mensual de Cuentas.
  GMAIL_SENDER   = mensajeria@santander.cl

Para generar la contraseña de aplicación:
  https://myaccount.google.com/apppasswords
"""
import email
import imaplib
import logging
import os
from dataclasses import dataclass
from email.message import Message

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


@dataclass
class RawCartola:
    uid: str
    fecha_recibido: str
    asunto: str
    remitente: str
    pdf_bytes: bytes
    pdf_filename: str


class GmailPoller:
    """
    Se conecta a Gmail por IMAP y descarga los correos de cartola
    que aún no han sido procesados.

    Uso:
        poller = GmailPoller()
        cartolas = poller.fetch_new()
        for c in cartolas:
            poller.mark_processed(c.uid)
    """

    def __init__(
        self,
        user: str | None = None,
        app_password: str | None = None,
        subject_filter: str | None = None,
        sender_filter: str | None = None,
    ):
        self.user = user or os.environ["GMAIL_USER"]
        self.app_password = app_password or os.environ["GMAIL_APP_PASS"]
        self.subject_filter = subject_filter or os.environ.get(
            "GMAIL_SUBJECT", "Cartola Mensual de Cuentas."
        )
        self.sender_filter = sender_filter or os.environ.get(
            "GMAIL_SENDER", "mensajeria@santander.cl"
        )
        # Label que le ponemos a los emails ya procesados
        self._processed_label = os.environ.get("GMAIL_PROCESSED_LABEL", "AppGastos/Processed")

    # ── API pública ───────────────────────────────────────────────────────────

    def fetch_new(self) -> list[RawCartola]:
        """Descarga los correos de cartola no procesados aún."""
        with self._connect() as conn:
            uids = self._search_unprocessed(conn)
            logger.info("Emails de cartola sin procesar: %d", len(uids))
            result = []
            for uid in uids:
                try:
                    raw = self._fetch_cartola(conn, uid)
                    if raw:
                        result.append(raw)
                except Exception as exc:
                    logger.error("Error procesando UID %s: %s", uid, exc)
            return result

    def debug_search(self, subject: str | None = None) -> dict:
        """Devuelve qué uids encuentra cada estrategia de search.
        Si pasas subject, lo usa como override del subject_filter."""
        original_subj = self.subject_filter
        if subject:
            self.subject_filter = subject
        result = {"subject_filter": self.subject_filter, "sender": self.sender_filter, "processed_label": self._processed_label}
        try:
            with self._connect() as conn:
                # 1) X-GM-RAW
                gm_query = (
                    f'from:{self.sender_filter} '
                    f'subject:"{self.subject_filter}" '
                    f'-label:{self._processed_label}'
                )
                try:
                    st, data = conn.uid("SEARCH", "X-GM-RAW", f'"{gm_query}"')
                    result["xgmraw"] = {"status": st, "uids": [u.decode() for u in (data[0].split() if data and data[0] else [])]}
                except Exception as exc:
                    result["xgmraw"] = {"error": str(exc)}

                # 2) X-GM-RAW sin -label
                gm_query2 = f'from:{self.sender_filter} subject:"{self.subject_filter}"'
                try:
                    st, data = conn.uid("SEARCH", "X-GM-RAW", f'"{gm_query2}"')
                    uids2 = [u.decode() for u in (data[0].split() if data and data[0] else [])]
                    result["xgmraw_all"] = {"status": st, "count": len(uids2), "uids": uids2[-20:]}
                except Exception as exc:
                    result["xgmraw_all"] = {"error": str(exc)}

                # 3) IMAP normal SUBJECT
                try:
                    crit = f'FROM "{self.sender_filter}" SUBJECT "{self.subject_filter}"'
                    st, data = conn.uid("SEARCH", None, crit)
                    uids3 = [u.decode() for u in (data[0].split() if data and data[0] else [])]
                    result["imap_subject"] = {"status": st, "count": len(uids3), "uids": uids3[-20:]}
                except Exception as exc:
                    result["imap_subject"] = {"error": str(exc)}

                # 4) Para uids encontrados en xgmraw_all, verificar sus labels actuales
                if result.get("xgmraw_all", {}).get("uids"):
                    sample = result["xgmraw_all"]["uids"][-5:]
                    labels = []
                    for uid in sample:
                        try:
                            st, data = conn.uid("FETCH", uid.encode(), "(X-GM-LABELS)")
                            labels.append({"uid": uid, "raw": str(data[0]) if data and data[0] else None})
                        except Exception as exc:
                            labels.append({"uid": uid, "error": str(exc)})
                    result["sample_labels"] = labels
        finally:
            self.subject_filter = original_subj
        return result

    def list_subjects(self, days: int = 60, sender_only: bool = True) -> list[dict]:
        """Devuelve subject/fecha/UID de los últimos N días desde el sender, sin filtrar por subject.
        Útil para diagnosticar qué emails existen cuando el filtro no matchea.
        """
        from datetime import datetime, timedelta
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
        with self._connect() as conn:
            if sender_only:
                criteria = f'FROM "{self.sender_filter}" SINCE {since}'
            else:
                criteria = f'SINCE {since}'
            status, data = conn.uid("SEARCH", None, criteria)
            if status != "OK" or not data[0]:
                return []
            uids = data[0].split()
            out = []
            for uid in uids[-50:]:  # cap en 50
                try:
                    st, msg_data = conn.uid("FETCH", uid, "(BODY[HEADER.FIELDS (SUBJECT FROM DATE)])")
                    if st != "OK":
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    out.append({
                        "uid": uid.decode(),
                        "subject": self._decode_header(msg.get("Subject", "")),
                        "from": self._decode_header(msg.get("From", "")),
                        "date": msg.get("Date", ""),
                    })
                except Exception as exc:
                    logger.error("Error fetching headers UID %s: %s", uid, exc)
            return out

    def mark_processed(self, uid: str) -> None:
        """Agrega el label 'AppGastos/Processed' al email para no volver a procesarlo."""
        with self._connect() as conn:
            # Crear el label si no existe (IMAP Gmail)
            conn.create(self._processed_label)
            uid_bytes = uid.encode() if isinstance(uid, str) else uid
            conn.uid("STORE", uid_bytes, "+X-GM-LABELS", f'"{self._processed_label}"')
            logger.info("Email %s marcado como procesado", uid)

    # ── internals ─────────────────────────────────────────────────────────────

    def _connect(self):
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(self.user, self.app_password)
        conn.select("INBOX")
        return conn

    def _search_unprocessed(self, conn) -> list[bytes]:
        """Busca UIDs de correos que:
        - Tienen el asunto esperado
        - Son del sender esperado
        - NO tienen el label de procesado

        Usa X-GM-RAW (extensión Gmail) que maneja acentos y la sintaxis nativa de Gmail.
        """
        # Sintaxis nativa de Gmail — maneja acentos correctamente
        gm_query = (
            f'from:{self.sender_filter} '
            f'subject:"{self.subject_filter}" '
            f'-label:{self._processed_label}'
        )
        try:
            status, data = conn.uid("SEARCH", "X-GM-RAW", f'"{gm_query}"')
            if status == "OK" and data[0]:
                return data[0].split()
        except imaplib.IMAP4.error as exc:
            logger.warning("X-GM-RAW search falló: %s. Probando IMAP plano.", exc)

        # Fallback 1: IMAP SEARCH normal (puede fallar con acentos)
        criteria = (
            f'FROM "{self.sender_filter}" '
            f'SUBJECT "{self.subject_filter}" '
            f'X-GM-LABELS NOT "{self._processed_label}"'
        )
        try:
            status, data = conn.uid("SEARCH", None, criteria)
            if status == "OK" and data[0]:
                return data[0].split()
        except imaplib.IMAP4.error:
            pass

        # Fallback 2: solo por sender, filtramos subject en Python después
        simple_criteria = f'FROM "{self.sender_filter}"'
        status, data = conn.uid("SEARCH", None, simple_criteria)
        if status != "OK" or not data[0]:
            return []
        all_uids = data[0].split()
        # Filtramos por subject en Python para evitar problemas de encoding
        filtered = []
        for uid in all_uids[-100:]:  # solo los últimos 100 para no demorar
            try:
                st, msg_data = conn.uid("FETCH", uid, "(BODY[HEADER.FIELDS (SUBJECT X-GM-LABELS)])")
                if st != "OK":
                    continue
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    header_text = raw.decode("utf-8", errors="replace")
                else:
                    header_text = str(raw)
                # Match flexible: tokens del subject_filter sin acentos
                subj_lower = header_text.lower()
                expected_tokens = [t.lower() for t in self.subject_filter.split()
                                   if len(t) >= 4 and t.lower() not in {'mensual', 'cuentas.', 'de'}]
                if not expected_tokens:
                    expected_tokens = [self.subject_filter.lower()]
                # Quitamos acentos para match más permisivo
                def _strip_acc(s):
                    return (s.replace('á','a').replace('é','e').replace('í','i')
                             .replace('ó','o').replace('ú','u').replace('ñ','n'))
                subj_norm = _strip_acc(subj_lower)
                if all(_strip_acc(t) in subj_norm for t in expected_tokens):
                    # Skip los que ya tienen el label de procesado
                    if self._processed_label.lower() not in subj_lower:
                        filtered.append(uid)
            except Exception:
                continue
        return filtered

    def _fetch_cartola(self, conn, uid: bytes) -> RawCartola | None:
        """Descarga un email y extrae el primer adjunto PDF."""
        status, msg_data = conn.uid("FETCH", uid, "(RFC822)")
        if status != "OK":
            return None

        raw_email = msg_data[0][1]
        msg: Message = email.message_from_bytes(raw_email)

        asunto = self._decode_header(msg.get("Subject", ""))
        remitente = self._decode_header(msg.get("From", ""))
        fecha = msg.get("Date", "")

        # Buscar adjunto PDF
        pdf_bytes = None
        pdf_filename = "cartola.pdf"
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disp = part.get("Content-Disposition", "")
            if content_type == "application/pdf" or (
                "attachment" in content_disp and part.get_filename("").endswith(".pdf")
            ):
                pdf_bytes = part.get_payload(decode=True)
                pdf_filename = part.get_filename(pdf_filename) or pdf_filename
                break

        if not pdf_bytes:
            logger.warning("Email UID %s no tiene adjunto PDF", uid.decode())
            return None

        logger.info(
            "Cartola encontrada: asunto=%r, remitente=%r, archivo=%r, tamaño=%d bytes",
            asunto, remitente, pdf_filename, len(pdf_bytes),
        )
        return RawCartola(
            uid=uid.decode(),
            fecha_recibido=fecha,
            asunto=asunto,
            remitente=remitente,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
        )

    @staticmethod
    def _decode_header(value: str) -> str:
        """Decodifica headers MIME encoded (ej: =?utf-8?..?)."""
        parts = email.header.decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return "".join(decoded)
