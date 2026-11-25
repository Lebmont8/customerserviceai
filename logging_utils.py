"""Central logging configuration with credential and PII redaction."""
import logging
import re


_REDACTIONS = [
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)(Crypto-Pay-API-Token[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+"),
    re.compile(r"(?i)(CLIENT_BOT_TOKEN|MANAGER_BOT_TOKEN|OPENAI_API_KEY|CRYPTOBOT_TOKEN)=\S+"),
]


def redact_text(value: object) -> str:
    text = str(value)
    for pattern in _REDACTIONS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = redact_text(rendered)
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the fully formatted record, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(level: int = logging.INFO):
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        level=level,
    )
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(
            RedactingFormatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
        )
        if not any(isinstance(flt, RedactingFilter) for flt in handler.filters):
            handler.addFilter(RedactingFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
