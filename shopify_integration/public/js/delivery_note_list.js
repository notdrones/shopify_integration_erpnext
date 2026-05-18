frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

// Capture ERPNext's built-in get_indicator BEFORE we override it so we can
// delegate back to it for non-Shopify DNs and special statuses (Return,
// Return Issued, Closed) without changing ERPNext's default behaviour at all.
const _erpnext_dn_indicator = frappe.listview_settings['Delivery Note'].get_indicator;

// Cached once per page load:
//   null  = not yet fetched (first render falls back to ERPNext default)
//   false = no Shopify store has enable_sales_invoice = 1
//   true  = at least one store has SI enabled → show Shopify indicators
let _shopify_si_active = null;

Object.assign(frappe.listview_settings['Delivery Note'], {
    // status and is_return are needed to detect Return / Return Issued / Closed
    add_fields: ["shopify_order_id", "per_billed", "status", "is_return"],

    onload: function(listview) {
        // Single server call — checks if any Shopify store has SI enabled.
        // Cached so repeated list refreshes don't re-query.
        frappe.call({
            method: 'shopify_integration.utils.sales_invoice.is_sales_invoice_enabled',
            callback: function(r) {
                const was_unset = (_shopify_si_active === null);
                _shopify_si_active = !!(r.message);
                // Refresh only when SI is active so Shopify indicators render.
                // When false, ERPNext defaults already show correctly.
                if (was_unset && _shopify_si_active) {
                    listview.refresh();
                }
            }
        });
    },

    get_indicator: function(doc) {
        // ── Non-Shopify DN or SI not enabled ──────────────────────────────────
        // Delegate entirely to ERPNext's original indicator — zero interference.
        if (!doc.shopify_order_id || !_shopify_si_active) {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        // ── Shopify DN with SI enabled ─────────────────────────────────────────
        // For special lifecycle statuses delegate to ERPNext so the correct
        // label and colour is shown (Return → gray, Return Issued → grey,
        // Closed → green). These are not billing states — "Shopify" label
        // would be misleading here.
        if (cint(doc.is_return) === 1 && doc.status === 'Return') {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }
        if (doc.status === 'Closed' || doc.status === 'Return Issued') {
            return _erpnext_dn_indicator ? _erpnext_dn_indicator(doc) : undefined;
        }

        // ── Normal Shopify DN: billing-aware "Shopify" indicator ───────────────
        // Orange = not yet billed  (needs invoice)
        // Yellow = partially billed
        // Green  = fully billed / completed
        const billed = flt(doc.per_billed || 0);
        if (billed >= 100) {
            return [__('Shopify'), 'green',  'shopify_order_id,is,set'];
        }
        if (billed > 0) {
            return [__('Shopify'), 'yellow', 'shopify_order_id,is,set'];
        }
        return     [__('Shopify'), 'orange', 'shopify_order_id,is,set'];
    }
});
