"""
e_compliance.py — Trigger e-Invoice and e-Waybill generation via India Compliance.

Called from sales_invoice.py after a Shopify Sales Invoice is submitted.
Both operations are enqueued as independent background jobs so a portal
error or IRP timeout never blocks or rolls back the Sales Invoice.

Conflict-safety: if GST Settings has auto_generate_e_invoice / auto_generate_e_waybill
enabled, India Compliance fires its own hooks on SI submit.  Our background jobs
check whether the document was already processed before making any IRP call, so
enabling both paths never results in duplicate portal requests.

Entry point:
  trigger_e_compliance_for_si(si_name, settings)
      Checks the two flags on Shopify Settings and enqueues the relevant jobs.
      No-ops silently when India Compliance is not installed or a flag is off.

Background jobs (called by RQ worker):
  _generate_e_invoice(si_name)
  _generate_e_waybill(si_name)
"""

import time

import frappe


def trigger_e_compliance_for_si(si_name: str, settings) -> None:
    """
    Enqueue e-Invoice and/or e-Waybill generation for a submitted Sales Invoice.

    Uses enqueue_after_commit=True so the SI is fully committed to the database
    before the IRP portal call starts — a portal failure can never roll back the SI.

    The job_name deduplication key prevents the same SI from being enqueued twice
    if the webhook fires more than once.

    :param si_name:  Submitted Sales Invoice name
    :param settings: Shopify Settings document for this store
    """
    if settings.get("enable_e_invoice"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_invoice",
            si_name=si_name,
            queue="default",
            timeout=120,
            job_name=f"shopify_einvoice_{si_name}",
            enqueue_after_commit=True,
        )

    if settings.get("enable_e_waybill"):
        frappe.enqueue(
            "shopify_integration.utils.e_compliance._generate_e_waybill",
            si_name=si_name,
            e_waybill_threshold=settings.get("e_waybill_threshold") or None,
            queue="default",
            timeout=120,
            job_name=f"shopify_ewaybill_{si_name}",
            enqueue_after_commit=True,
        )


# ── Background jobs ────────────────────────────────────────────────────────────

def _generate_e_invoice(si_name: str) -> None:
    """
    Background job: generate an e-Invoice for a submitted Sales Invoice.

    Guards:
    - Skips B2C invoices (gst_category not in B2B / SEZ / Deemed Export) — IRP
      rejects these and India Compliance would create a spurious Integration Request.
    - Skips if an IRN is already set on the SI, meaning India Compliance's own
      auto_generate_e_invoice hook already ran.  This prevents duplicate IRP calls
      when both Shopify Settings and GST Settings auto-generation are enabled.
    """
    si_data = frappe.db.get_value(
        "Sales Invoice",
        si_name,
        ["gst_category", "irn"],
        as_dict=True,
    )
    if not si_data:
        return

    # Only B2B transactions are eligible for e-Invoice.
    if (si_data.gst_category or "") not in (
        "B2B", "SEZ With Payment", "SEZ Without Payment", "Deemed Export"
    ):
        frappe.logger().info(
            f"Shopify: e-Invoice skipped for {si_name} — "
            f"gst_category '{si_data.gst_category}' is not eligible"
        )
        return

    # Already generated — India Compliance sets irn on the SI after a successful
    # IRP call.  Skip to avoid a duplicate portal request and Integration Request log.
    if si_data.irn:
        frappe.logger().info(
            f"Shopify: e-Invoice skipped for {si_name} — IRN already present ({si_data.irn})"
        )
        return

    try:
        from india_compliance.gst_india.utils.e_invoice import generate_e_invoice
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Invoice generation via Shopify Integration.",
            f"Shopify: e-Invoice Skipped (app missing) — {si_name}",
        )
        return

    _MAX_RETRIES = 3
    for _attempt in range(_MAX_RETRIES):
        try:
            generate_e_invoice(si_name, throw=False)
            frappe.logger().info(
                f"Shopify: e-Invoice generation triggered for Sales Invoice {si_name}"
            )
            break
        except frappe.QueryDeadlockError:
            if _attempt >= _MAX_RETRIES - 1:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify: e-Invoice Generation Failed (deadlock) — {si_name}",
                )
                raise  # let RQ mark the job failed — not a silent success
            frappe.db.rollback()
            time.sleep(0.5 * (_attempt + 1))
            # IRP call may have succeeded before the deadlock hit db_set.
            # Re-read irn — if now set, India Compliance wrote it in another
            # transaction; nothing more to do.
            if frappe.db.get_value("Sales Invoice", si_name, "irn"):
                frappe.logger().info(
                    f"Shopify: e-Invoice deadlock resolved for {si_name} — IRN now present"
                )
                break
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Shopify: e-Invoice Generation Failed — {si_name}",
            )
            break


def _generate_e_waybill(si_name: str, e_waybill_threshold=None) -> None:
    """
    Background job: generate an e-Waybill for a submitted Sales Invoice.

    Guards:
    - If e_waybill_threshold is set in Shopify Settings, skips the IRP call when
      the SI's grand_total is below that value.  Protects against a misconfigured
      GST Settings threshold (e.g. ₹0) triggering e-Waybills for low-value orders.
      When e_waybill_threshold is None, the threshold check is delegated to India
      Compliance (which reads from GST Settings).
    - Skips if an E Waybill Log already exists for this SI, meaning India
      Compliance's own auto_generate_e_waybill hook already ran.  This prevents
      duplicate IRP calls when both Shopify Settings and GST Settings
      auto-generation are enabled.
    - All other eligibility checks (inter/intra-state, HSN, etc.) are enforced by
      India Compliance internally.
    """
    # Shopify Settings threshold guard — only active when explicitly configured.
    if e_waybill_threshold:
        grand_total = frappe.db.get_value("Sales Invoice", si_name, "grand_total") or 0
        if grand_total < e_waybill_threshold:
            frappe.logger().info(
                f"Shopify: e-Waybill skipped for {si_name} — "
                f"grand_total {grand_total} is below Shopify threshold {e_waybill_threshold}"
            )
            return

    # Already generated — India Compliance creates an E Waybill Log record after a
    # successful IRP call.  Skip to avoid a duplicate portal request.
    already_generated = frappe.db.exists(
        "E Waybill Log",
        {"reference_name": si_name, "doctype_name": "Sales Invoice"},
    )
    if already_generated:
        frappe.logger().info(
            f"Shopify: e-Waybill skipped for {si_name} — E Waybill Log already exists"
        )
        return

    try:
        from india_compliance.gst_india.utils.e_waybill import generate_e_waybill
    except ImportError:
        frappe.log_error(
            "India Compliance app is not installed. "
            "Install it to enable e-Waybill generation via Shopify Integration.",
            f"Shopify: e-Waybill Skipped (app missing) — {si_name}",
        )
        return

    _MAX_RETRIES = 3
    for _attempt in range(_MAX_RETRIES):
        try:
            generate_e_waybill(doctype="Sales Invoice", docname=si_name)
            frappe.logger().info(
                f"Shopify: e-Waybill generation triggered for Sales Invoice {si_name}"
            )
            break
        except frappe.QueryDeadlockError:
            if _attempt >= _MAX_RETRIES - 1:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify: e-Waybill Generation Failed (deadlock) — {si_name}",
                )
                raise  # let RQ mark the job failed — not a silent success
            frappe.db.rollback()
            time.sleep(0.5 * (_attempt + 1))
            # The IRP call may have succeeded before the deadlock hit db_set.
            # If an E Waybill Log now exists, the result was committed in another
            # transaction — safe to stop retrying.
            if frappe.db.exists(
                "E Waybill Log",
                {"reference_name": si_name, "doctype_name": "Sales Invoice"},
            ):
                frappe.logger().info(
                    f"Shopify: e-Waybill deadlock resolved for {si_name} — E Waybill Log now exists"
                )
                break
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Shopify: e-Waybill Generation Failed — {si_name}",
            )
            break
