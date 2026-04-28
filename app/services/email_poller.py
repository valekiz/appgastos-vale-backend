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
        """
        criteria = (
            f'FROM "{self.sender_filter}" '
            f'SUBJECT "{self.subject_filter}" '
            f'X-GM-LABELS NOT "{self._processed_label}"'
        )
        try:
            status, data = conn.uid("SEARCH", None, criteria)
        except imaplib.IMAP4.error:
            # Fallback sin X-GM-LABELS si el servidor no lo soporta
            simple_criteria = f'FROM "{self.sender_filter}" SUBJECT "{self.subject_filter}"'
            status, data = conn.uid("SEARCH", None, simple_criteria)

        if status != "OK" or not data[0]:
            return []
        return data[0].split()

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
