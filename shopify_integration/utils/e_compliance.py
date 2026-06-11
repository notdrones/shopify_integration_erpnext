"""
e_compliance.py — Trigger e-Invoice and e-Waybill generation via India Compliance.

Called from sales_invoice.py after a Shopify Sales Invoice is submitted.
Both operations are enqueued as independent background jobs so a portal
error or IRP timeout never blocks or rolls back the Sales Invoice.

Entry point:
  trigger_e_compliance_for_si(si_name, settings)
      Checks the two flags on Shopify Settings and enqueues the relevant jobs.
      No-ops silently when India Compliance is not installed or a flag is off.

Background jobs (called by RQ worker):
  _generate_e_invoice(si_name)
  _generate_e_waybill(si_name)
"""

import frappe


def trigger_e_compliance_for_si(si_name: str, settings) -> None:
    """
    Enqueue e-Invoice and/or e-Waybill generation for a submitted Sales Invoice.

    Uses enqueue_after_commit=True so the SI is fully committed to the database
    before the IRP portal call starts — a portal failure can never roll back the SI.

    :param si_name:  Submitted Sales Invoice name
    :param settings: Shopify Settings document for this store
    """
    if settings.get("enable_e_invoice"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_invoice",
            si_name=si_name,
            queue="default",
            timeout=120,
            enqueue_after_commit=True,
        )

    if settings.get("enable_e_waybill"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_waybill",
            si_name=si_name,
            queue="default",
            timeout=120,
            enqueue_after_commit=True,
        )


# ── Background jobs ────────────────────────────────────────────────────────────

def _generate_e_invoice(si_name: str) -> None:
    """
    Background job: generate an e-Invoice for a submitted Sales Invoice.

    Calls India Compliance's generate_e_invoice() with throw=False so invoices
    that are ineligible (company not enrolled, B2C customer, below threshold)
    exit silently without writing to the Error Log.  Real portal errors are
    caught and logged separately.
    """
    try:
        from india_compliance.gst_india.utils.e_invoice import generate_e_invoice
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Invoice generation via Shopify Integration.",
            f"Shopify: e-Invoice Skipped (app missing) — {si_name}",
        )
        return

    try:
        generate_e_invoice(si_name, throw=False)
        frappe.logger().info(
            f"Shopify: e-Invoice generation triggered for Sales Invoice {si_name}"
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: e-Invoice Generation Failed — {si_name}",
        )


def _generate_e_waybill(si_name: str) -> None:
    """
    Background job: generate an e-Waybill for a submitted Sales Invoice.

    Generates Part A of the e-Waybill (shipment details).  Part B (transporter /
    vehicle details) must be updated manually via the e-Waybill menu on the SI
    form once the shipment is assigned.

    India Compliance checks e-waybill eligibility internally (value threshold,
    inter/intra-state, etc.) — ineligible documents raise an exception which is
    caught and logged.
    """
    try:
        from india_compliance.gst_india.utils.e_waybill import generate_e_waybill
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Waybill generation via Shopify Integration.",
            f"Shopify: e-Waybill Skipped (app missing) — {si_name}",
        )
        return

    try:
        generate_e_waybill(doctype="Sales Invoice", docname=si_name)
        frappe.logger().info(
            f"Shopify: e-Waybill generation triggered for Sales Invoice {si_name}"
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: e-Waybill Generation Failed — {si_name}",
        )
