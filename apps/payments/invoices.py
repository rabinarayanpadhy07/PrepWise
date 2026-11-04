"""Invoice PDFs, rendered on demand from the immutable Invoice snapshot."""

from django.utils import timezone

from common.pdf import Document


def format_amount(amount_minor, currency):
    return f"{currency} {amount_minor / 100:,.2f}"


def render_invoice_pdf(invoice):
    doc = Document(f"Invoice {invoice.number}")
    doc.title("Invoice")
    doc.key_values(
        [
            ("Invoice number", invoice.number),
            ("Issued", timezone.localtime(invoice.issued_at).strftime("%d %b %Y")),
        ]
    )

    doc.heading("From")
    doc.paragraph(invoice.seller_name)
    if invoice.seller_address:
        doc.paragraph(invoice.seller_address)
    if invoice.seller_email:
        doc.paragraph(invoice.seller_email)
    if invoice.seller_tax_id:
        doc.paragraph(f"Tax ID: {invoice.seller_tax_id}")

    doc.heading("Billed to")
    if invoice.buyer_name:
        doc.paragraph(invoice.buyer_name)
    doc.paragraph(invoice.buyer_email)

    doc.heading("Details")
    start = timezone.localtime(invoice.period_start).strftime("%d %b %Y")
    end = timezone.localtime(invoice.period_end).strftime("%d %b %Y")
    doc.key_values(
        [
            ("Item", f"{invoice.plan_name} (one-time payment, manual renewal)"),
            ("Access period", f"{start} to {end} ({invoice.duration_days} days)"),
            ("Amount paid", format_amount(invoice.amount_minor, invoice.currency)),
            ("Payment reference", f"{invoice.gateway.title()} {invoice.gateway_payment_id}"),
        ]
    )
    doc.rule()
    doc.muted(
        "No tax has been calculated or itemised on this document. The amount shown is the "
        "amount charged. This purchase does not renew automatically."
    )
    doc.muted(
        "Refunds, if any, are listed in your payment history and are not reflected on this invoice."
    )
    return doc.output()
