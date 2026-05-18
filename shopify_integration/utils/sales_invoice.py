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


# ── Doc-event: Delivery Note on_submit ────────────────────────────────────────

def create_si_from_dn_on_submit(doc, method):
    """
    Delivery Note on_submit hook.  Only acts when si_dn_timing == "Immediate".
    Enqueues a background job (enqueue_after_commit=True) so the DN submit
    transaction commits before the SI job starts — safe and non-blocking.
    """
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
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Immediate Sales Invoice from DN Failed — {dn_name}",
        )


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
        si.submit()

    frappe.db.commit()  # nosemgrep: frappe-manual-commit — runs in scheduler/background job; SI must persist independently
    return si.name
