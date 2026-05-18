app_name        = "shopify_integration"
app_title       = "Shopify Integration"
app_publisher   = "Yash Chaurasia"
app_description = "Shopify to ERPNext integration with automatic Sales Orders, Payment Entries, Sales Invoices, and India GST compliance."
app_email       = "chaurasiayash351@gmail.com"
app_license     = "GPLv3"
app_version     = "1.0.0"
app_color       = "#96BF48"
app_icon        = "octicon octicon-package"

required_apps   = ["frappe", "erpnext"]

# ----------------------------------------------------------
# DocType JavaScript — loaded only when viewing that DocType
# ----------------------------------------------------------
doctype_js = {
    "Delivery Note": "public/js/delivery_note.js",
}

doctype_list_js = {
    "Delivery Note": "public/js/delivery_note_list.js",
}

# ----------------------------------------------------------
# Install / Uninstall hooks
# ----------------------------------------------------------
after_install  = "shopify_integration.install.after_install"
before_uninstall = "shopify_integration.install.before_uninstall"

# ----------------------------------------------------------
# DocType event hooks
# ----------------------------------------------------------
doc_events = {
    "Sales Order": {
        # Clear Shopify fields on amended copies so duplicate-check is not
        # blocked and manual amendments are not linked to Shopify orders.
        "before_insert": "shopify_integration.utils.sales_order.clear_shopify_fields_on_amend",
        # Clear Shopify Log reference before deletion so ERPNext link-validation
        # does not block Sales Order deletion.
        "on_trash": "shopify_integration.utils.sales_order.clear_shopify_log_on_trash",
    },
    "Delivery Note": {
        # Frappe supports a list of handlers for a single event.
        # Each handler is called in order; a failure in one does NOT block others.
        "on_submit": [
            # Immediate SI creation — no-ops when si_dn_timing != "Immediate".
            "shopify_integration.utils.sales_invoice.create_si_from_dn_on_submit",
            # Fulfillment push — no-ops when enable_fulfillment_push = 0
            # or fulfillment_trigger != "On DN Submit".
            "shopify_integration.utils.fulfillment.on_delivery_note_submit",
        ],
    },
}

# ----------------------------------------------------------
# Scheduler jobs
# ----------------------------------------------------------
scheduler_events = {
    "hourly": [
        # "After Delivery Note" mode: find submitted DNs that have no SI yet
        # and create Sales Invoices for them.
        "shopify_integration.utils.scheduler.create_invoices_after_delivery_note",
    ],
    "daily": [
        # Delete Shopify Logs older than `shopify_log_retention_days` days.
        "shopify_integration.utils.scheduler.delete_old_shopify_logs",
    ],
}

# ----------------------------------------------------------
# Whitelisted API endpoint for Shopify webhooks
# Accessed via: /api/method/shopify_integration.api.shopify_webhook
# ----------------------------------------------------------
