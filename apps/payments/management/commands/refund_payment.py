from django.core.management.base import BaseCommand, CommandError

from apps.payments import services
from apps.payments.gateway import PaymentGatewayError, PaymentsUnavailable
from apps.payments.models import Payment


class Command(BaseCommand):
    help = (
        "Refund a captured payment through the gateway. Without --amount the remaining "
        "amount is refunded, which revokes the subscription it paid for."
    )

    def add_arguments(self, parser):
        parser.add_argument("payment_id", help="Gateway payment ID, e.g. pay_...")
        parser.add_argument("--amount", type=int, help="Amount in minor units (paise).")
        parser.add_argument("--reason", default="", help="Internal note stored with the refund.")

    def handle(self, *args, payment_id, amount=None, reason="", **options):
        payment = Payment.objects.filter(gateway_payment_id=payment_id).first()
        if payment is None:
            raise CommandError("No payment with that ID.")
        try:
            refund = services.request_refund(payment, amount_minor=amount, reason=reason)
        except (ValueError, PaymentGatewayError, PaymentsUnavailable) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Refund {refund.gateway_refund_id}: {refund.amount_minor} {refund.currency} "
            f"({refund.status})."
        )
