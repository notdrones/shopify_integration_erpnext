import frappe


def before_uninstall():
    """
    Remove custom fields added by this app when it is uninstalled.
    Prevents orphaned fields from cluttering the Customer and Sales Order forms.
    """
    _SHOPIFY_CUSTOM_FIELDS = [
        # Customer
        "Customer-shopify_section",
        "Customer-shopify_customer_id",
        "Customer-shopify_phone",
        "Customer-shopify_email",
        # Sales Order
        "Sales Order-shopify_section",
        "Sales Order-shopify_order_id",
        "Sales Order-shopify_store",
        # Sales Order Item
        "Sales Order Item-shopify_line_item_id",
        # Delivery Note
        "Delivery Note-shopify_section",
        "Delivery Note-shopify_order_id",
        "Delivery Note-shopify_store",
        "Delivery Note-shopify_fulfillment_id",
        "Delivery Note-shopify_fulfillment_status",
        "Delivery Note-shopify_fulfillment_error",
        "Delivery Note-shopify_tracking_number",
        "Delivery Note-shopify_tracking_url",
        "Delivery Note-shopify_tracking_company",
    ]
    for cf_name in _SHOPIFY_CUSTOM_FIELDS:
        if frappe.db.exists("Custom Field", cf_name):
            try:
                frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Shopify Integration: Could not remove custom field {cf_name} on uninstall"
                )
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — uninstall hook runs outside request lifecycle
    print("🗑️  Shopify Integration: Custom fields removed.")


def after_install():
    """
    Create / update all custom fields required for Shopify Integration.
    Safe to re-run on reinstall — create_or_update corrects existing fields
    (unique removed, insert_after moved, collapsible added) without losing data.
    Compatible with ERPNext v15 and v16.
    """
    _cleanup_deprecated_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — install hook; deprecated fields must be removed before creating new ones

    create_customer_custom_fields()
    create_sales_order_custom_fields()
    create_sales_order_item_custom_fields()
    create_delivery_note_custom_fields()
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — install hook runs outside request lifecycle
    print("✅ Shopify Integration: Custom fields created / updated successfully.")


# ── Cleanup ────────────────────────────────────────────────────────────────────

def _cleanup_deprecated_fields():
    """Remove fields that are no longer used by this app."""
    deprecated = [
        # Item fields — SKU matched via item_code, tax via Item Tax Template rows
        "Item-shopify_sku",
        "Item-shopify_tax_template",
        "Item-shopify_section",
        # Sales Order — shopify_order_name is redundant; value already in po_no
        "Sales Order-shopify_order_name",
    ]
    for cf_name in deprecated:
        if frappe.db.exists("Custom Field", cf_name):
            frappe.delete_doc("Custom Field", cf_name, ignore_permissions=True)
            print(f"  Removed deprecated custom field: {cf_name}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def create_or_update_custom_field(doctype, field_def):
    """
    Create a custom field if it doesn't exist; update if it does.
    Ensures reinstalls correct field properties without removing existing data.
    """
    fieldname = field_def.get("fieldname")
    cf_name   = f"{doctype}-{fieldname}"

    if frappe.db.exists("Custom Field", cf_name):
        cf      = frappe.get_doc("Custom Field", cf_name)
        changed = False
        for key, value in field_def.items():
            if str(cf.get(key) or "") != str(value or ""):
                cf.set(key, value)
                changed = True
        if changed:
            cf.save(ignore_permissions=True)
    else:
        cf = frappe.get_doc({"doctype": "Custom Field", "dt": doctype, **field_def})
        cf.insert(ignore_permissions=True)


def _so_shopify_anchor() -> str:
    """
    Find the best insert_after anchor for the Shopify section in Sales Order.
    Tries several stable field names in order of preference so the section
    lands in More Info → Additional Info regardless of ERPNext version.
    """
    so_meta = frappe.get_meta("Sales Order")
    for fieldname in [
        "campaign",                      # ERPNext v15 More Info → Additional Info
        "inter_company_order_reference", # v14/v15 Additional Info
        "source",                        # very stable fallback
        "tc_name",                       # Terms section fallback
        "amendment_date",                # absolute last resort
    ]:
        if so_meta.get_field(fieldname):
            return fieldname
    return "amendment_date"


# ── Customer custom fields ─────────────────────────────────────────────────────

def create_customer_custom_fields():
    """Add collapsible Shopify section to Customer DocType."""
    doctype = "Customer"

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": "customer_details",
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_customer_id",
        "label":        "Shopify Customer ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_phone",
        "label":        "Shopify Phone",
        "fieldtype":    "Data",
        "insert_after": "shopify_customer_id",
        "read_only":    1,
        "description":  "Phone number used as the primary unique identifier for customer matching.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_email",
        "label":        "Shopify Email",
        "fieldtype":    "Data",
        "insert_after": "shopify_phone",
        "read_only":    1,
    })


# ── Sales Order custom fields ──────────────────────────────────────────────────

def create_sales_order_custom_fields():
    """
    Add collapsible Shopify reference section to Sales Order.

    Placed in More Info → Additional Info (after 'campaign' or nearest stable field).
    unique is NOT set on shopify_order_id — uniqueness is enforced in code,
    excluding cancelled orders, so cancel-and-amend workflows are not blocked.
    shopify_order_name is not created — the value already lives in po_no.
    """
    doctype = "Sales Order"
    anchor  = _so_shopify_anchor()

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_order_id",
        "label":        "Shopify Order ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
        "description":  "Numeric Shopify order ID. Used for duplicate detection (cancelled orders excluded).",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_store",
        "label":        "Shopify Store",
        "fieldtype":    "Data",
        "insert_after": "shopify_order_id",
        "read_only":    1,
        "description":  "Shop domain e.g. notdrones.myshopify.com.",
    })


# ── Delivery Note custom fields ──────────────────────────────────────────────────

def create_delivery_note_custom_fields():
    """
    Add collapsible Shopify reference section to Delivery Note.
    This allows fields to map from Sales Order -> Delivery Note automatically,
    which is required for list view indicators.
    """
    doctype = "Delivery Note"
    
    # Try to find a good anchor, default to amendment_date
    dn_meta = frappe.get_meta(doctype)
    anchor = "amendment_date"
    for fieldname in ["inter_company_order_reference", "source", "tc_name"]:
        if dn_meta.get_field(fieldname):
            anchor = fieldname
            break

    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_section",
        "label":        "Shopify",
        "fieldtype":    "Section Break",
        "insert_after": anchor,
        "collapsible":  1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_order_id",
        "label":        "Shopify Order ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_section",
        "read_only":    1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_store",
        "label":        "Shopify Store",
        "fieldtype":    "Data",
        "insert_after": "shopify_order_id",
        "read_only":    1,
    })

    # ── Fulfillment status fields (read-only, written by integration) ───────────
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_fulfillment_id",
        "label":        "Shopify Fulfillment ID",
        "fieldtype":    "Data",
        "insert_after": "shopify_store",
        "read_only":    1,
        "description":  "Set automatically after a fulfillment is pushed to Shopify.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_fulfillment_status",
        "label":        "Fulfillment Status",
        "fieldtype":    "Select",
        "options":      "\nPending\nFulfilled\nPartially Fulfilled\nFailed\nSkipped",
        "insert_after": "shopify_fulfillment_id",
        "read_only":    1,
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_fulfillment_error",
        "label":        "Fulfillment Error",
        "fieldtype":    "Small Text",
        "insert_after": "shopify_fulfillment_status",
        "read_only":    1,
        "description":  "Last error message when fulfillment push failed. Clear manually after resolving.",
    })

    # ── Tracking fields (editable by staff before/after submission) ─────────────
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_tracking_number",
        "label":        "Tracking Number",
        "fieldtype":    "Data",
        "insert_after": "shopify_fulfillment_error",
        "description":  "Courier tracking / AWB number. Sent to Shopify with the fulfillment and included in the customer notification email.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_tracking_url",
        "label":        "Tracking URL",
        "fieldtype":    "Data",
        "insert_after": "shopify_tracking_number",
        "description":  "Full tracking URL (e.g. https://track.delhivery.com/?id=123). Required when Carrier is 'Other'. For recognised carriers Shopify generates the URL automatically.",
    })
    create_or_update_custom_field(doctype, {
        "fieldname":    "shopify_tracking_company",
        "label":        "Carrier",
        "fieldtype":    "Select",
        "options":      (
            "\nDelhivery\nBlueDart\nDTDC\nEkart\nIndia Post"
            "\nShadowfax\nXpressbees\nEcom Express\nAmazon Logistics"
            "\nSmartr Logistics\nPickrr\nNimbuspost\nShypmax"
            "\nDHL Express\nDHL eCommerce\nDHL eCommerce Asia"
            "\nFedEx\nUPS\nTNT\nSF Express\nChina Post"
            "\nDPD\nRoyal Mail\nAustralia Post\nCanada Post"
            "\nUSPS\n4PX\nSendle\nEvri\nOther"
        ),
        "insert_after": "shopify_tracking_url",
        "description":  "Select the courier. For carriers in the list, Shopify auto-generates the tracking URL. Select 'Other' and provide the full Tracking URL manually for unlisted couriers.",
    })


# ── Sales Order Item custom fields ─────────────────────────────────────────────

def create_sales_order_item_custom_fields():
    """
    Add shopify_line_item_id to Sales Order Item rows.
    This stores the Shopify numeric line_item.id during webhook processing so
    the fulfillment module can match DN items back to Shopify fulfillment order
    line items without relying on fragile SKU comparisons.
    """
    create_or_update_custom_field("Sales Order Item", {
        "fieldname":    "shopify_line_item_id",
        "label":        "Shopify Line Item ID",
        "fieldtype":    "Data",
        "insert_after": "item_tax_template",
        "read_only":    1,
        "description":  "Shopify order line_item.id. Set automatically during webhook processing. Used for fulfillment matching.",
    })
