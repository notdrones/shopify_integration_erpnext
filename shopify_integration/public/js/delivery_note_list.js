frappe.listview_settings['Delivery Note'] = frappe.listview_settings['Delivery Note'] || {};

Object.assign(frappe.listview_settings['Delivery Note'], {
    add_fields: ["shopify_order_id", "shopify_fulfillment_status"],

    // get_indicator runs during row render — reliable across all Frappe versions.
    // For non-Shopify DNs (shopify_order_id absent) we return nothing so Frappe
    // falls back to its default status indicator (To Deliver, Completed, etc.).
    get_indicator: function(doc) {
        if (!doc.shopify_order_id) return;

        const status = doc.shopify_fulfillment_status || '';

        if (status === 'Fulfilled') {
            return [__('Fulfilled'), 'blue',   'shopify_fulfillment_status,=,Fulfilled'];
        }
        if (status === 'Partially Fulfilled') {
            return [__('Partial'), 'orange',   'shopify_fulfillment_status,=,Partially Fulfilled'];
        }
        if (status === 'Failed') {
            return [__('Sync Failed'), 'red',  'shopify_fulfillment_status,=,Failed'];
        }

        // Pending / Skipped / not yet pushed — generic "Shopify" green badge
        return [__('Shopify'), 'green', 'shopify_order_id,=,' + doc.shopify_order_id];
    }
});
