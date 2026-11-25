"""P1A billing abstraction.

Only prepares payment requests. Real card and manual-invoice collection remain
disabled until a provider is selected and live acceptance is approved.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation


SUPPORTED_PROVIDERS = {"cryptobot", "manual_invoice", "card_placeholder"}


@dataclass(frozen=True)
class BillingRequest:
    shop_id: str
    provider: str
    amount: str
    currency: str
    description: str
    collectable: bool

    def to_dict(self) -> dict:
        return asdict(self)


def prepare_billing_request(
    shop_id: str,
    provider: str,
    amount: str | float,
    currency: str = "USD",
    description: str = "CustomerServiceAI subscription",
) -> BillingRequest:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_billing_provider")
    try:
        normalized_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_amount") from exc
    if normalized_amount <= 0:
        raise ValueError("invalid_amount")
    normalized_currency = (currency or "").strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("invalid_currency")
    return BillingRequest(
        shop_id=shop_id,
        provider=provider,
        amount=str(normalized_amount),
        currency=normalized_currency,
        description=(description or "")[:200],
        collectable=provider == "cryptobot",
    )
