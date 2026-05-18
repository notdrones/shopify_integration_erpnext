frappe.ui.form.on('Shopify Settings', {

    refresh: function (frm) {
        // "Test Connection" button — verifies the access_token by calling
        // GET /shop.json.  Only shown when the record is saved (has a name).
        if (frm.doc.name && frm.doc.shop_domain) {
            frm.add_custom_button(__('Test Shopify Connection'), function () {
                frappe.xcall(
                    'shopify_integration.utils.shopify_api.test_shopify_connection',
                    { store_name: frm.doc.name }
                ).then(function (result) {
                    frappe.msgprint({
                        title:     result.success ? __('Connection Successful') : __('Connection Failed'),
                        message:   result.message,
                        indicator: result.success ? 'green' : 'red',
                    });
                });
            }, __('Actions'));
        }

        // Populate Sales Order naming series from ERPNext meta
        frappe.call({
            method: 'shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings.get_naming_series',
            args: { doctype: 'Sales Order' },
            callback: function (r) {
                if (r.message) {
                    var options = r.message.split('\n').filter(Boolean);
                    // Blank first option → fall back to ERPNext default series
                    options.unshift('');
                    frm.set_df_property('naming_series', 'options', options);
                    frm.refresh_field('naming_series');
                }
            },
            error: function () {
                frappe.msgprint({
                    title: __('Warning'),
                    message: __('Could not load Sales Order naming series options. Please refresh the page.'),
                    indicator: 'orange'
                });
            }
        });

        // Populate Customer naming series from ERPNext meta
        frappe.call({
            method: 'shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings.get_naming_series',
            args: { doctype: 'Customer' },
            callback: function (r) {
                if (r.message) {
                    var options = r.message.split('\n').filter(Boolean);
                    // Blank first option → use ERPNext default Customer series
                    options.unshift('');
                    frm.set_df_property('customer_naming_series', 'options', options);
                    frm.refresh_field('customer_naming_series');
                }
            },
            error: function () {
                frappe.msgprint({
                    title: __('Warning'),
                    message: __('Could not load Customer naming series options. Please refresh the page.'),
                    indicator: 'orange'
                });
            }
        });

        // Populate Payment Entry naming series from ERPNext meta
        frappe.call({
            method: 'shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings.get_naming_series',
            args: { doctype: 'Payment Entry' },
            callback: function (r) {
                if (r.message) {
                    var options = r.message.split('\n').filter(Boolean);
                    options.unshift('');
                    frm.set_df_property('pe_naming_series', 'options', options);
                    frm.refresh_field('pe_naming_series');
                }
            },
            error: function () {
                frappe.msgprint({
                    title: __('Warning'),
                    message: __('Could not load Payment Entry naming series options. Please refresh the page.'),
                    indicator: 'orange'
                });
            }
        });

        // Populate Sales Invoice naming series from ERPNext meta
        frappe.call({
            method: 'shopify_integration.shopify_integration.doctype.shopify_settings.shopify_settings.get_naming_series',
            args: { doctype: 'Sales Invoice' },
            callback: function (r) {
                if (r.message) {
                    var options = r.message.split('\n').filter(Boolean);
                    options.unshift('');
                    frm.set_df_property('si_naming_series', 'options', options);
                    frm.refresh_field('si_naming_series');
                }
            },
            error: function () {
                frappe.msgprint({
                    title: __('Warning'),
                    message: __('Could not load Sales Invoice naming series options. Please refresh the page.'),
                    indicator: 'orange'
                });
            }
        });

        // Apply account filters (Bank / Cash only, no group accounts)
        _apply_account_filters(frm);
    },

    company: function (frm) {
        // Re-apply filters when company changes so company scoping stays correct
        _apply_account_filters(frm);
    }

});

/**
 * Restrict Bank / Cash account pickers to:
 *   - ledger accounts only (is_group = 0)
 *   - account_type in ('Bank', 'Cash')
 *   - not disabled
 *   - matching the selected company (if any)
 *
 * This prevents accidentally selecting a GROUP account — ERPNext won't stop
 * a Payment Entry from being submitted against a group account in some flows,
 * and once submitted it's painful to reverse.
 */
function _apply_account_filters(frm) {
    const account_filters = function () {
        const filters = {
            is_group:     0,
            account_type: ['in', ['Bank', 'Cash']],
            disabled:     0,
        };
        if (frm.doc.company) {
            filters.company = frm.doc.company;
        }
        return { filters: filters };
    };

    // Default Bank / Cash Account on the parent form
    frm.set_query('default_bank_account', account_filters);

    // Bank / Cash Account on each row of the Gateway Mapping child table
    frm.set_query('bank_account', 'payment_gateway_mapping', account_filters);
}
