"""
sales_invoice.py — Create ERPNext Sales Invoice from Shopify-generated documents.

Two entry points for creation:

  create_sales_invoice_from_so(so, settings)
      Used by the "After Payment Entry" flow (Option B).
      Creates the SI directly from the submitted Sales Order — no Delivery Note
      required.  Called from sales_order.py immediately after a successful PE.

  create_sales_invoice_from_dn(dn_name, settings)
      Used by the scheduler for the "After Delivery Note / Scheduled" flow and
      by the on_submit immediate flow.  Creates the SI from a submitted Delivery
      Note so stock movement and billing are properly linked.

Both functions raise on error so the caller can log appropriately.

Utility entry points (whitelisted for JS):

  get_dn_shopify_invoice_status(dn_name)
      Single server call used by the Delivery Note form JS to decide whether to
      show the auto-SI banner.  Avoids querying Sales Invoice Item (a child
      DocType) through frappe.client, which throws a PermissionError.

Doc-event handler:

  create_si_from_dn_on_submit(doc, method)
      Delivery Note on_submit hook — fires only when si_dn_timing == "Immediate".
      Enqueues a background job so the form submit is not blocked.
"""

import frappe


# ── Public helpers for the form JS ────────────────────────────────────────────

@frappe.whitelist()
def is_sales_invoice_enabled() -> bool:
    """
    Return True if at least one Shopify Settings document has
    enable_sales_invoice = 1.  Called once per list-view load by
    delivery_note_list.js to decide whether to show Shopify indicators.
    """
    return bool(frappe.db.exists("Shopify Settings", {"enable_sales_invoice": 1}))


@frappe.whitelist()
def get_dn_shopify_invoice_status(dn_name: str) -> dict:
    """
    Return a dict with all info the Delivery Note form JS needs to show or hide
    the auto-SI banner.  One server round-trip instead of three.

    Returns {} when this DN is not linked to a Shopify order that has
    auto-SI-after-DN enabled — the JS treats any falsy/empty result as "nothing
    to show".
    """
    # Return DNs never get an auto-SI — nothing to show on the form.
    if frappe.db.get_value("Delivery Note", dn_name, "is_return"):
        return {}

    # Find SO linked from any DN item
    so_name = frappe.db.get_value(
        "Delivery Note Item",
        {"parent": dn_name, "against_sales_order": ["!=", ""]},
        "against_sales_order",
    )
    if not so_name:
        return {}

    so_data = frappe.db.get_value(
        "Sales Order",
        so_name,
        ["shopify_order_id", "shopify_store"],
        as_dict=True,
    )
    if not so_data or not so_data.shopify_order_id:
        return {}

    settings = frappe.db.get_value(
        "Shopify Settings",
        {
            "shop_domain": so_data.shopify_store,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Delivery Note",
        },
        ["name", "si_dn_timing", "si_dn_delay_hours"],
        as_dict=True,
    )
    if not settings:
        return {}

    # Check for an existing non-cancelled SI — must query via parent (Sales Invoice)
    # because Sales Invoice Item is a child DocType and cannot be queried directly
    # through frappe.client (which would raise "not a valid parent DocType").
    has_si = bool(frappe.db.sql(
        """
        SELECT 1
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE sii.delivery_note = %s
          AND si.docstatus != 2
        LIMIT 1
        """,
        dn_name,
    ))

    return {
        "is_shopify":  True,
        "si_timing":   settings.si_dn_timing or "Scheduled",
        "delay_hours": int(settings.si_dn_delay_hours or 0),
        "has_si":      has_si,
    }


@frappe.whitelist()
def create_si_from_dn_manual(dn_name: str) -> dict:
    """
    Whitelist: manually trigger Sales Invoice creation for a submitted Shopify
    Delivery Note.  Used as a fallback from the DN form when auto-creation has
    failed or not yet run.

    Returns:
      {"queued": True}               — job enqueued, check back in a few seconds
      {"already_exists": True, "si_name": "..."}  — SI already there
    Raises frappe.ValidationError for invalid states.
    """
    dn_vals = frappe.db.get_value(
        "Delivery Note", dn_name, ["docstatus", "is_return"], as_dict=True
    )
    if not dn_vals:
        frappe.throw(f"Delivery Note {dn_name} not found.")
    if dn_vals.docstatus != 1:
        frappe.throw("The Delivery Note must be submitted before a Sales Invoice can be created.")
    if dn_vals.is_return:
        frappe.throw("Return Delivery Notes do not generate Sales Invoices.")

    so_name = frappe.db.get_value(
        "Delivery Note Item",
        {"parent": dn_name, "against_sales_order": ["!=", ""]},
        "against_sales_order",
    )
    if not so_name:
        frappe.throw("No linked Sales Order found on this Delivery Note — cannot determine Shopify store.")

    shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store")
    if not shopify_store:
        frappe.throw("This Delivery Note is not linked to a Shopify order.")

    settings_name = frappe.db.get_value(
        "Shopify Settings",
        {
            "shop_domain": shopify_store,
            "enable_sync": 1,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Delivery Note",
        },
        "name",
    )
    if not settings_name:
        frappe.throw(
            "No active Shopify Settings found with Sales Invoice → After Delivery Note enabled "
            "for this store. Check Shopify Settings → Sales Invoice tab."
        )

    _existing = frappe.db.sql(
        """
        SELECT si.name FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE sii.delivery_note = %s AND si.docstatus != 2 AND si.is_return = 0
        LIMIT 1
        """,
        dn_name,
    )
    if _existing:
        return {"already_exists": True, "si_name": _existing[0][0]}

    frappe.enqueue(
        "shopify_integration.utils.sales_invoice._create_si_for_dn_immediate",
        dn_name=dn_name,
        store_name=settings_name,
        queue="short",
    )
    return {"queued": True}


# ── Doc-event: Delivery Note on_submit ────────────────────────────────────────

def create_si_from_dn_on_submit(doc, method):
    """
    Delivery Note on_submit hook.  Only acts when si_dn_timing == "Immediate".
    Enqueues a background job (enqueue_after_commit=True) so the DN submit
    transaction commits before the SI job starts — safe and non-blocking.
    """
    # Return DNs (stock returns) must never trigger auto-SI creation — they are
    # handled separately as Credit Notes via the refunds/create webhook.
    if doc.get("is_return"):
        return

    # Resolve the linked Shopify SO via DN items (more reliable than reading
    # doc.shopify_store, which may not be populated on all DN header rows).
    so_name = None
    for item in (doc.get("items") or []):
        if item.get("against_sales_order"):
            so_name = item.against_sales_order
            break
    if not so_name:
        return

    shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store")
    if not shopify_store:
        return

    settings_name = frappe.db.get_value(
        "Shopify Settings",
        {
            "shop_domain": shopify_store,
            "enable_sync": 1,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Delivery Note",
            "si_dn_timing": "Immediate",
        },
        "name",
    )
    if not settings_name:
        return

    frappe.enqueue(
        "shopify_integration.utils.sales_invoice._create_si_for_dn_immediate",
        dn_name=doc.name,
        store_name=settings_name,
        enqueue_after_commit=True,
        queue="short",
    )


def _create_si_for_dn_immediate(dn_name: str, store_name: str):
    """Background job for immediate SI creation triggered by DN submission."""
    settings = frappe.get_doc("Shopify Settings", store_name)
    try:
        si_name = create_sales_invoice_from_dn(dn_name, settings)
        frappe.logger().info(
            f"Shopify: immediate Sales Invoice {si_name} created from DN {dn_name}"
        )
    except Exception:
        tb = frappe.get_traceback()
        frappe.log_error(tb, f"Shopify: Immediate Sales Invoice from DN Failed — {dn_name}")
        _send_si_failure_email(settings, "Delivery Note", dn_name, tb)


# ── SI creation functions ──────────────────────────────────────────────────────

def create_sales_invoice_from_so(so, settings, pe_name: str = None) -> str:
    """
    Create a Sales Invoice directly from a submitted Sales Order.

    :param so:       Submitted ERPNext Sales Order document
    :param settings: Shopify Settings document
    :return:         Sales Invoice name
    :raises:         Any exception from ERPNext SI creation (caller logs it)
    """
    from erpnext.selling.doctype.sales_order.sales_order import (
        make_sales_invoice as so_to_si,
    )

    # Idempotency guard: if a non-cancelled SI already exists for this SO
    # (e.g. from a prior run or a manual retry), return it instead of creating
    # a duplicate.  The scheduler path has an equivalent NOT EXISTS SQL check;
    # this mirrors that protection for the "After Payment Entry" path.
    _existing = frappe.db.sql(
        """
        SELECT si.name
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE sii.sales_order = %s
          AND si.docstatus != 2
          AND si.is_return  = 0
        LIMIT 1
        """,
        so.name,
    )
    if _existing:
        frappe.logger().info(
            f"Shopify: Sales Invoice {_existing[0][0]} already exists for SO {so.name} — skipping"
        )
        return _existing[0][0]

    # ERPNext's make_sales_invoice calls get_mapped_doc which calls
    # check_permission() before ignore_permissions is applied.  In a webhook
    # context frappe.session.user may be Guest.  Swap to Administrator for
    # the duration of this call (same pattern as set_missing_values in SO).
    _prev_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        si = so_to_si(so.name)
    finally:
        frappe.session.user = _prev_user

    # Copy payment terms from the Sales Order (ERPNext's mapper may skip this
    # in some versions — explicit copy guarantees consistency).
    if so.get("payment_terms_template"):
        si.payment_terms_template = so.payment_terms_template
    if so.get("payment_schedule"):
        si.payment_schedule = []  # let ERPNext regenerate from template

    if settings.get("cost_center"):
        si.cost_center = settings.cost_center
        for item in si.items:
            item.cost_center = settings.cost_center

    if settings.get("si_naming_series"):
        si.naming_series = settings.si_naming_series

    # Ensure grand_total is computed before advance allocation.
    # make_sales_invoice calls calculate_taxes_and_totals internally, but
    # running it again after cost_center overrides is cheap and safe.
    si.run_method("calculate_taxes_and_totals")

    # Always enable FIFO advance allocation so any Payment Entry
    # (whether created by our integration or manually) is automatically
    # linked to this Sales Invoice on submit.
    si.allocate_advances_automatically = 1

    si.flags.ignore_permissions = True
    si.insert()

    if settings.get("auto_submit_sales_invoice"):
        si.flags.ignore_permissions = True
        si.submit()
        _trigger_e_compliance(si.name, settings)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in background job; SI must persist for advance allocation
    return si.name


def create_sales_invoice_from_dn(dn_name: str, settings) -> str:
    """
    Create a Sales Invoice from a submitted Delivery Note.

    :param dn_name:  Delivery Note document name
    :param settings: Shopify Settings document
    :return:         Sales Invoice name
    :raises:         Any exception from ERPNext SI creation (caller logs it)
    """
    from erpnext.stock.doctype.delivery_note.delivery_note import (
        make_sales_invoice as dn_to_si,
    )

    # Idempotency guard: mirrors the NOT EXISTS check in the scheduler SQL.
    # The immediate on_submit path (enqueued job) can fire more than once
    # if the DN is somehow submitted twice, so guard here rather than relying
    # solely on the enqueue job_name deduplication.
    _existing = frappe.db.sql(
        """
        SELECT si.name
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE sii.delivery_note = %s
          AND si.docstatus != 2
          AND si.is_return  = 0
        LIMIT 1
        """,
        dn_name,
    )
    if _existing:
        frappe.logger().info(
            f"Shopify: Sales Invoice {_existing[0][0]} already exists for DN {dn_name} — skipping"
        )
        return _existing[0][0]

    _prev_user = frappe.session.user
    try:
        if frappe.session.user in ("Guest", None, ""):
            frappe.session.user = "Administrator"
        si = dn_to_si(dn_name)
    finally:
        frappe.session.user = _prev_user

    if settings.get("cost_center"):
        si.cost_center = settings.cost_center
        for item in si.items:
            item.cost_center = settings.cost_center

    if settings.get("si_naming_series"):
        si.naming_series = settings.si_naming_series

    # Always enable FIFO advance allocation so any Payment Entry
    # linked to the Sales Order is automatically allocated to this SI.
    si.allocate_advances_automatically = 1

    si.flags.ignore_permissions = True
    si.insert()

    if settings.get("auto_submit_sales_invoice"):
        si.flags.ignore_permissions = True
        _MAX_SUBMIT_RETRIES = 3
        for _attempt in range(_MAX_SUBMIT_RETRIES):
            try:
                si.submit()
                break
            except frappe.QueryDeadlockError:
                if _attempt >= _MAX_SUBMIT_RETRIES - 1:
                    raise
                frappe.db.rollback()
                import time; time.sleep(0.4 * (_attempt + 1))
                si.reload()
        _trigger_e_compliance(si.name, settings)

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in scheduler/background job; SI must persist independently
    return si.name


# ── Private helpers ────────────────────────────────────────────────────────────

def _trigger_e_compliance(si_name: str, settings) -> None:
    """Enqueue e-Invoice / e-Waybill jobs if either flag is on in settings."""
    if not (settings.get("enable_e_invoice") or settings.get("enable_e_waybill")):
        return
    from shopify_integration.utils.e_compliance import trigger_e_compliance_for_si
    trigger_e_compliance_for_si(si_name, settings)


def _send_si_failure_email(settings, reference_type: str, reference_name: str, error_message: str) -> None:
    """
    Send a notification email when Sales Invoice auto-creation fails.
    Mirrors the PE failure email pattern in sales_order.py.
    No-ops when failure_email_to is not configured in settings.
    """
    to_emails = (settings.get("failure_email_to") or "").strip()
    if not to_emails:
        return

    shop = settings.get("shop_domain") or settings.get("name") or "Shopify"
    cc_emails = (settings.get("failure_email_cc") or "").strip()
    cc_list = [e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else []
    subject = f"[Shopify] Sales Invoice Failed — {reference_type} {reference_name} ({shop})"

    message = f"""
    <p>The Shopify Integration could not create a <b>Sales Invoice</b> automatically.</p>
    <p>Please create it manually in ERPNext using the steps below.</p>
    <table border="0" cellpadding="4" style="font-family:Arial;font-size:13px;border-collapse:collapse;">
      <tr><td style="padding:4px 12px 4px 0;"><b>Reference</b></td><td>{reference_type}: <b>{reference_name}</b></td></tr>
      <tr><td style="padding:4px 12px 4px 0;"><b>Store</b></td><td>{shop}</td></tr>
    </table>
    <br>
    <p><b>How to create the Sales Invoice manually:</b></p>
    <ol style="font-family:Arial;font-size:13px;line-height:1.8;">
      <li>Open the <b>{reference_type}</b> <code>{reference_name}</code> in ERPNext.</li>
      <li>Click <b>Create &rarr; Sales Invoice</b>.</li>
    </ol>
    <p><b>Failure reason:</b></p>
    <pre style="background:#fef2f2;padding:10px;border-left:4px solid #ef4444;font-size:12px;white-space:pre-wrap;">{error_message[:2000]}</pre>
    <p style="color:#6b7280;font-size:12px;">
      See ERPNext &rarr; Error Log for the full traceback.
    </p>
    """
    try:
        frappe.sendmail(
            recipients=[e.strip() for e in to_emails.split(",") if e.strip()],
            cc=cc_list,
            subject=subject,
            message=message,
            delayed=False,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Shopify: SI Failure Email Send Error")
