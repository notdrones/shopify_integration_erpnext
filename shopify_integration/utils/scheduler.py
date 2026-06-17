"""
scheduler.py — Background jobs for Shopify Integration.

Registered in hooks.py under scheduler_events → hourly.

create_invoices_after_delivery_note()
    For every active Shopify Settings record that has:
      - enable_sales_invoice = 1
      - sales_invoice_trigger = "After Delivery Note"

    Find all submitted Delivery Notes whose items link to a Shopify Sales Order
    from that store, where no Sales Invoice has been made from the DN yet, then
    create the Sales Invoice (and optionally submit it).

    Runs hourly.  Each SI creation is wrapped in its own try/except so one
    failed DN does not block the rest of the batch.
"""

import frappe


def create_invoices_after_delivery_note():
    """
    Hourly scheduler entry point.  Processes all active stores configured for
    'After Delivery Note' invoice creation with si_dn_timing == 'Scheduled'.

    Stores set to 'Immediate' are excluded here — their SI is created by the
    Delivery Note on_submit hook (create_si_from_dn_on_submit) instead.
    """
    active_stores = frappe.get_all(
        "Shopify Settings",
        filters={
            "enable_sync": 1,
            "enable_sales_invoice": 1,
            "sales_invoice_trigger": "After Delivery Note",
            "si_dn_timing": ["!=", "Immediate"],
        },
        pluck="name",
    )

    for store_name in active_stores:
        try:
            settings = frappe.get_doc("Shopify Settings", store_name)
            _process_store(settings)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Shopify: Scheduler Error for store {store_name}",
            )


def _process_store(settings):
    """
    For one store: find DNs with Shopify-SO items that have no SI yet, and
    create a Sales Invoice for each.

    Query logic:
      - Delivery Note is submitted (docstatus = 1)
      - At least one DN item links back to a submitted Sales Order whose
        shopify_store matches this settings record
      - Delay check (primary): Activity Log 'Submit' entry for the DN is at
        least si_dn_delay_hours old — the true submission timestamp, unaffected
        by any post-submit edits (transporter details, e-Waybill fields, etc.)
        Fallback: when no Submit entry exists in Activity Log (disabled site,
        purged logs, scripted submission), dn.creation + 2h buffer is used so
        the DN is never permanently excluded.
      - No submitted (or draft) Sales Invoice already references this DN
    """
    delay_hours = int(settings.get("si_dn_delay_hours") or 0)

    # Compute the delay cutoffs in Python using the site timezone, then compare
    # them against the stored timestamps in SQL.
    #
    # Frappe stores `creation` / Activity Log `creation` in the site timezone
    # (System Settings → Time Zone, e.g. Asia/Kolkata).  MariaDB's NOW(),
    # however, returns the database server time, which on Frappe Cloud is UTC.
    # Comparing a stored IST timestamp against NOW() therefore understates a
    # document's age by the UTC offset (5.5h for IST), so DNs were only picked
    # up ~5.5h later than the configured delay.  now_datetime() returns the
    # site-timezone "now", so cutoff vs stored-timestamp is an apples-to-apples
    # comparison regardless of the database server timezone.
    from frappe.utils import add_to_date, now_datetime

    now = now_datetime()
    submit_cutoff = add_to_date(now, hours=-delay_hours)            # al.creation <= this
    creation_cutoff = add_to_date(now, hours=-(delay_hours + 2))    # dn.creation <= this

    dn_rows = frappe.db.sql(
        """
        SELECT DISTINCT dn.name AS dn_name
        FROM `tabDelivery Note` dn
        JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
        JOIN `tabSales Order` so ON so.name = dni.against_sales_order
        WHERE dn.docstatus  = 1
          AND dn.is_return  = 0
          AND so.docstatus  = 1
          AND so.shopify_store = %(store)s
          AND (
              -- Primary: exact submission time from Activity Log.
              EXISTS (
                  SELECT 1 FROM `tabActivity Log` al
                  WHERE al.reference_doctype = 'Delivery Note'
                    AND al.reference_name = dn.name
                    AND al.operation = 'Submit'
                    AND al.creation <= %(submit_cutoff)s
              )
              -- Fallback: no Activity Log Submit row (logs disabled/purged/scripted submit).
              -- Use dn.creation with a 2-hour buffer so DNs are never permanently excluded.
              OR (
                  NOT EXISTS (
                      SELECT 1 FROM `tabActivity Log` al2
                      WHERE al2.reference_doctype = 'Delivery Note'
                        AND al2.reference_name = dn.name
                        AND al2.operation = 'Submit'
                  )
                  AND dn.creation <= %(creation_cutoff)s
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM `tabSales Invoice Item` sii
              JOIN `tabSales Invoice` si ON si.name = sii.parent
              WHERE sii.delivery_note = dn.name
                AND si.docstatus != 2
          )
        """,
        {
            "store": settings.shop_domain,
            "submit_cutoff": submit_cutoff,
            "creation_cutoff": creation_cutoff,
        },
        as_dict=True,
    )

    for row in dn_rows:
        _create_si_for_dn(row["dn_name"], settings)


def _create_si_for_dn(dn_name: str, settings):
    """Create (and optionally submit) a Sales Invoice from one Delivery Note."""
    try:
        from shopify_integration.utils.sales_invoice import create_sales_invoice_from_dn

        si_name = create_sales_invoice_from_dn(dn_name, settings)
        frappe.logger().info(
            f"Shopify scheduler: created Sales Invoice {si_name} from DN {dn_name}"
        )
    except Exception:
        tb = frappe.get_traceback()
        frappe.log_error(tb, f"Shopify: Sales Invoice from DN Failed — {dn_name}")
        from shopify_integration.utils.sales_invoice import _send_si_failure_email
        _send_si_failure_email(settings, "Delivery Note", dn_name, tb)


def delete_old_shopify_logs():
    """
    Daily scheduler entry point.  Deletes Shopify Logs older than
    `shopify_log_retention_days` days for each configured store.
    Skips stores where the setting is 0 or blank (keep all logs).
    """
    from frappe.utils import add_days, today

    active_stores = frappe.get_all(
        "Shopify Settings",
        filters={"enable_sync": 1},
        fields=["name", "shop_domain", "shopify_log_retention_days"],
    )

    for store in active_stores:
        days = store.get("shopify_log_retention_days") or 0
        if not days or days <= 0:
            continue

        cutoff = add_days(today(), -days)
        old_logs = frappe.get_all(
            "Shopify Log",
            filters={"shop_domain": store.get("shop_domain") or store["name"], "creation": ["<", cutoff]},
            pluck="name",
        )

        deleted = 0
        for log_name in old_logs:
            try:
                frappe.delete_doc("Shopify Log", log_name, force=True, ignore_permissions=True, delete_permanently=True)
                deleted += 1
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"Shopify: Log Deletion Failed — {log_name}")

        if deleted:
            frappe.db.commit()  # nosemgrep: frappe-manual-commit — batch deletion needs intermediate commits per store
            frappe.logger().info(f"Shopify: deleted {deleted} old logs for store {store['name']}")
