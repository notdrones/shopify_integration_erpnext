"""
fulfillment.py — Push ERPNext Delivery Note as a Shopify fulfillment.

Entry points
────────────
on_delivery_note_submit(doc, method)
    Delivery Note on_submit doc event.  Enqueues a background job when
    fulfillment push is configured as "On DN Submit".  Non-blocking — DN
    submission is never prevented by a Shopify API failure.

manual_push_fulfillment(dn_name)      [whitelisted]
    Called from the "Push Fulfillment to Shopify" button on the DN form.
    Also used as the retry path when status == "Failed".

update_fulfillment_tracking(dn_name)  [whitelisted]
    Called from the "Update Tracking on Shopify" button.  Sends a PUT to
    Shopify to attach / update tracking info on an already-created fulfillment.

get_dn_fulfillment_status(dn_name)    [whitelisted]
    Single server call used by the DN form JS to render the fulfillment
    status badge and decide which buttons to show.

Fulfillment flow (internal)
───────────────────────────
push_fulfillment_to_shopify(dn, settings)
  1. Guard checks (idempotency, access token, open fulfillment orders)
  2. GET /orders/{shopify_order_id}/fulfillment_orders.json
  3. Map DN items → fulfillment order line items
     Primary key:  shopify_line_item_id on SO item == line_item_id on FO line item
     Fallback key: variant_id / SKU (for orders created before this feature)
  4. Build fulfillment payload
  5. POST /fulfillments.json
  6. Persist result to DN (fulfillment_id, status)
  7. Log

Item matching detail
────────────────────
Each Delivery Note item row carries so_detail (the SO item row name).
The SO item row carries shopify_line_item_id (stored during webhook processing).
Shopify fulfillment order line items carry:
  id            — the fulfillment_order_line_item_id  (used in the POST payload)
  line_item_id  — the original order line_item.id     (matches shopify_line_item_id)
  variant_id    — product variant id                  (fallback matching)
  sku           — product SKU                         (last-resort fallback)
  fulfillable_quantity — remaining qty available      (must be >= DN qty)
"""

import frappe
from frappe.utils import flt

from shopify_integration.utils.shopify_api import ShopifyAPIClient, ShopifyAPIError


# ── Carrier options (must match Shopify's supported carrier names exactly) ─────
# These are also the options on the shopify_tracking_company custom field.
# Shopify auto-generates tracking URLs for recognised carriers.

CARRIER_OPTIONS = [
    "",
    # India
    "Delhivery", "BlueDart", "DTDC", "Ekart", "India Post",
    "Shadowfax", "Xpressbees", "Ecom Express", "Amazon Logistics",
    "Smartr Logistics", "Pickrr", "Nimbuspost", "Shypmax",
    # International
    "DHL Express", "DHL eCommerce", "DHL eCommerce Asia",
    "FedEx", "UPS", "TNT", "SF Express", "China Post",
    "DPD", "Royal Mail", "Australia Post", "Canada Post",
    "USPS", "4PX", "Sendle", "Evri",
    # Generic
    "Other",
]


# ── Doc event hook ─────────────────────────────────────────────────────────────

def on_delivery_note_submit(doc, method):
    """
    Delivery Note on_submit hook.  Enqueues a background fulfillment job
    only when trigger == 'On DN Submit'.  Returns immediately so the DN
    submit transaction is not blocked.
    """
    so_name = _get_linked_so(doc)
    if not so_name:
        return

    shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store")
    if not shopify_store:
        return

    settings_name = frappe.db.get_value(
        "Shopify Settings",
        {
            "shop_domain":              shopify_store,
            "enable_sync":              1,
            "enable_fulfillment_push":  1,
            "fulfillment_trigger":      "On DN Submit",
        },
        "name",
    )
    if not settings_name:
        return

    frappe.enqueue(
        "shopify_integration.utils.fulfillment._push_fulfillment_background",
        dn_name=doc.name,
        store_name=settings_name,
        enqueue_after_commit=True,
        queue="short",
    )


def _push_fulfillment_background(dn_name: str, store_name: str):
    """Background job wrapper — loads settings and calls the main push function."""
    settings = frappe.get_doc("Shopify Settings", store_name)
    dn       = frappe.get_doc("Delivery Note", dn_name)
    push_fulfillment_to_shopify(dn, settings)


# ── Whitelisted entry points (called from JS) ──────────────────────────────────

@frappe.whitelist()
def manual_push_fulfillment(dn_name: str) -> dict:
    """
    Push fulfillment from the DN form button.
    Also the retry path when status == "Failed".
    Returns {"success": bool, "message": str}.
    """
    dn = frappe.get_doc("Delivery Note", dn_name)

    # Resolve settings via linked SO
    so_name = _get_linked_so(dn)
    if not so_name:
        return {"success": False, "message": "This Delivery Note is not linked to a Shopify Sales Order."}

    shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store")
    settings_name = frappe.db.get_value(
        "Shopify Settings",
        {"shop_domain": shopify_store, "enable_sync": 1, "enable_fulfillment_push": 1},
        "name",
    )
    if not settings_name:
        return {"success": False, "message": "Fulfillment push is not enabled in Shopify Settings for this store."}

    settings = frappe.get_doc("Shopify Settings", settings_name)

    try:
        push_fulfillment_to_shopify(dn, settings)
        status = frappe.db.get_value("Delivery Note", dn_name, "shopify_fulfillment_status")
        if status in ("Fulfilled", "Partially Fulfilled"):
            return {"success": True, "message": f"Fulfillment pushed successfully. Status: {status}."}
        else:
            error = frappe.db.get_value("Delivery Note", dn_name, "shopify_fulfillment_error") or ""
            return {"success": False, "message": f"Fulfillment failed: {error}"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@frappe.whitelist()
def update_fulfillment_tracking(dn_name: str) -> dict:
    """
    Update tracking info on an already-created Shopify fulfillment.
    Called when the user changes tracking fields after the fulfillment was pushed.
    """
    dn = frappe.get_doc("Delivery Note", dn_name)
    fulfillment_id = dn.get("shopify_fulfillment_id")
    if not fulfillment_id:
        return {"success": False, "message": "No Shopify fulfillment ID on record — push fulfillment first."}

    so_name = _get_linked_so(dn)
    shopify_store = frappe.db.get_value("Sales Order", so_name, "shopify_store") if so_name else ""
    settings_name = frappe.db.get_value(
        "Shopify Settings",
        {"shop_domain": shopify_store, "enable_sync": 1, "enable_fulfillment_push": 1},
        "name",
    ) if shopify_store else ""
    if not settings_name:
        return {"success": False, "message": "Could not find active Shopify Settings for this store."}

    settings = frappe.get_doc("Shopify Settings", settings_name)
    client   = ShopifyAPIClient(settings)

    tracking_info = _build_tracking_info(dn)
    if not tracking_info:
        return {"success": False, "message": "No tracking information to update (Tracking Number is empty)."}

    try:
        client.put(
            f"fulfillments/{fulfillment_id}/update_tracking.json",
            {
                "fulfillment": {
                    "tracking_info":   tracking_info,
                    "notify_customer": bool(settings.get("notify_customer")),
                }
            },
        )
        return {"success": True, "message": "Tracking information updated on Shopify."}
    except ShopifyAPIError as exc:
        return {"success": False, "message": f"Shopify API error {exc.status_code}: {exc.errors}"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


@frappe.whitelist()
def get_dn_fulfillment_status(dn_name: str) -> dict:
    """
    Single server call for the DN form JS.
    Returns all information needed to render the fulfillment UI section.
    Returns {} when this DN is not a Shopify-linked DN with fulfillment enabled.
    """
    so_name = frappe.db.get_value(
        "Delivery Note Item",
        {"parent": dn_name, "against_sales_order": ["!=", ""]},
        "against_sales_order",
    )
    if not so_name:
        return {}

    so_data = frappe.db.get_value(
        "Sales Order", so_name, ["shopify_order_id", "shopify_store"], as_dict=True
    )
    if not so_data or not so_data.shopify_order_id:
        return {}

    settings = frappe.db.get_value(
        "Shopify Settings",
        {
            "shop_domain":             so_data.shopify_store,
            "enable_sync":             1,
            "enable_fulfillment_push": 1,
        },
        ["name", "fulfillment_trigger", "notify_customer"],
        as_dict=True,
    )
    if not settings:
        return {}

    dn_data = frappe.db.get_value(
        "Delivery Note",
        dn_name,
        [
            "shopify_fulfillment_id",
            "shopify_fulfillment_status",
            "shopify_fulfillment_error",
            "shopify_tracking_number",
            "shopify_tracking_url",
            "shopify_tracking_company",
        ],
        as_dict=True,
    ) or {}

    return {
        "is_shopify":           True,
        "fulfillment_trigger":  settings.fulfillment_trigger or "On DN Submit",
        "fulfillment_id":       dn_data.get("shopify_fulfillment_id") or "",
        "fulfillment_status":   dn_data.get("shopify_fulfillment_status") or "Pending",
        "fulfillment_error":    dn_data.get("shopify_fulfillment_error") or "",
        "has_tracking":         bool(dn_data.get("shopify_tracking_number")),
    }


# ── Core fulfillment logic ─────────────────────────────────────────────────────

def push_fulfillment_to_shopify(dn, settings):
    """
    Main fulfillment push function.  Safe to call from both the background job
    (on_submit trigger) and the whitelisted manual_push_fulfillment endpoint.

    All exceptions are caught internally.  Status and error are persisted to the
    DN custom fields.  This function never raises — callers can check
    shopify_fulfillment_status after the call returns.

    :param dn:       Delivery Note document (frappe.get_doc result)
    :param settings: Shopify Settings document (frappe.get_doc result)
    """
    dn_name = dn.name

    # ── Guard: idempotency ────────────────────────────────────────────────────
    existing_fulfillment_id = frappe.db.get_value(
        "Delivery Note", dn_name, "shopify_fulfillment_id"
    )
    if existing_fulfillment_id:
        frappe.logger().info(
            f"Shopify fulfillment: skipping {dn_name} — already pushed "
            f"(fulfillment_id={existing_fulfillment_id})"
        )
        return

    # ── Resolve Shopify order ID ───────────────────────────────────────────────
    so_name = _get_linked_so(dn)
    if not so_name:
        _save_result(dn_name, status="Skipped",
                     error="No Sales Order linked to this Delivery Note.")
        return

    shopify_order_id = frappe.db.get_value("Sales Order", so_name, "shopify_order_id")
    if not shopify_order_id:
        _save_result(dn_name, status="Skipped",
                     error="Linked Sales Order has no Shopify Order ID.")
        return

    try:
        client = ShopifyAPIClient(settings)

        # ── Step 1: GET fulfillment orders ────────────────────────────────────
        fo_response = client.get(
            f"orders/{shopify_order_id}/fulfillment_orders.json"
        )
        fulfillment_orders = [
            fo for fo in (fo_response.get("fulfillment_orders") or [])
            if fo.get("status") == "open"
        ]

        if not fulfillment_orders:
            _save_result(dn_name, status="Skipped",
                         error="No open fulfillment orders found on Shopify — order may already be fully fulfilled.")
            return

        # ── Step 2: Map DN items → fulfillment order line items ───────────────
        payload_lines, skipped_items = _build_line_items_payload(
            dn, so_name, fulfillment_orders
        )

        if not payload_lines:
            _save_result(dn_name, status="Skipped",
                         error="None of the Delivery Note items could be matched to open Shopify fulfillment order line items.")
            return

        # ── Step 3: Build and POST fulfillment ────────────────────────────────
        fulfillment_body = {
            "fulfillment": {
                "line_items_by_fulfillment_order": payload_lines,
                "notify_customer": bool(settings.get("notify_customer")),
            }
        }

        tracking_info = _build_tracking_info(dn)
        if tracking_info:
            fulfillment_body["fulfillment"]["tracking_info"] = tracking_info

        response   = client.post("fulfillments.json", fulfillment_body)
        fulfillment = response.get("fulfillment", {})
        fulfillment_id = str(fulfillment.get("id") or "")

        # ── Step 4: Persist result ────────────────────────────────────────────
        status = "Partially Fulfilled" if skipped_items else "Fulfilled"
        _save_result(dn_name, fulfillment_id=fulfillment_id, status=status)

        if skipped_items:
            frappe.log_error(
                f"Delivery Note {dn_name}: fulfillment pushed but some items "
                f"could not be matched and were skipped:\n"
                + "\n".join(f"  • {i}" for i in skipped_items),
                "Shopify: Partial Fulfillment Warning",
            )

        frappe.logger().info(
            f"Shopify: fulfillment {fulfillment_id} created for DN {dn_name} "
            f"(status={status})"
        )

    except ShopifyAPIError as exc:
        # 422: item already fulfilled / invalid request
        if exc.status_code == 422:
            _save_result(dn_name, status="Skipped",
                         error=f"Shopify rejected the fulfillment (422): {exc.errors}")
        else:
            error_msg = f"Shopify API error {exc.status_code}: {exc.errors}"
            _save_result(dn_name, status="Failed", error=error_msg)
            frappe.log_error(
                frappe.get_traceback(),
                f"Shopify: Fulfillment Push Failed — {dn_name}",
            )

    except Exception:
        _save_result(dn_name, status="Failed",
                     error="Unexpected error — see Error Log for details.")
        frappe.log_error(
            frappe.get_traceback(),
            f"Shopify: Fulfillment Push Failed — {dn_name}",
        )


# ── Item mapping helpers ───────────────────────────────────────────────────────

def _build_line_items_payload(dn, so_name: str, fulfillment_orders: list):
    """
    Map Delivery Note items to Shopify fulfillment order line items.

    Returns:
        payload_lines  — list of {fulfillment_order_id, fulfillment_order_line_items}
                         ready for the POST /fulfillments.json body
        skipped_items  — list of item_code strings that could not be matched
    """
    # Build a lookup: shopify_line_item_id → SO item details
    # We join through so_detail to get the SO item row for each DN item.
    so_item_map = {}   # shopify_line_item_id → {item_code, qty_in_dn}

    for dn_item in (dn.get("items") or []):
        so_detail = dn_item.get("so_detail")
        if not so_detail:
            continue

        so_line_item_id = frappe.db.get_value(
            "Sales Order Item", so_detail, "shopify_line_item_id"
        ) or ""

        key = so_line_item_id or f"__sku__{dn_item.item_code}"
        so_item_map.setdefault(key, {
            "item_code":         dn_item.item_code,
            "shopify_line_item_id": so_line_item_id,
            "qty":               0,
        })
        so_item_map[key]["qty"] = flt(so_item_map[key]["qty"]) + flt(dn_item.qty)

    # Walk fulfillment orders and match line items
    # Structure: {fulfillment_order_id: [(fo_line_item_id, qty), ...]}
    matched_by_fo = {}
    skipped_items = []

    for dn_key, dn_info in so_item_map.items():
        shopify_line_item_id = dn_info["shopify_line_item_id"]
        dn_qty               = int(dn_info["qty"])
        item_code            = dn_info["item_code"]

        fo_id, fo_line_item_id = _find_fo_line_item(
            fulfillment_orders,
            shopify_line_item_id=shopify_line_item_id,
            sku_fallback=item_code,
            required_qty=dn_qty,
        )

        if fo_line_item_id is None:
            skipped_items.append(item_code)
            continue

        matched_by_fo.setdefault(fo_id, [])
        matched_by_fo[fo_id].append({"id": fo_line_item_id, "quantity": dn_qty})

    payload_lines = [
        {
            "fulfillment_order_id":         fo_id,
            "fulfillment_order_line_items": line_items,
        }
        for fo_id, line_items in matched_by_fo.items()
    ]

    return payload_lines, skipped_items


def _find_fo_line_item(
    fulfillment_orders: list,
    shopify_line_item_id: str,
    sku_fallback: str,
    required_qty: int,
):
    """
    Find the (fulfillment_order_id, fulfillment_order_line_item_id) for a
    given Shopify line item ID.

    Matching priority:
      1. line_item_id == shopify_line_item_id  (exact, stored during SO creation)
      2. sku == sku_fallback                   (for orders before this feature)

    Returns (fo_id, fo_line_item_id) or (None, None) if not found / no qty.
    """
    # Pass 1: exact line_item_id match
    if shopify_line_item_id:
        for fo in fulfillment_orders:
            for li in (fo.get("line_items") or []):
                if (
                    str(li.get("line_item_id") or "") == shopify_line_item_id
                    and int(li.get("fulfillable_quantity") or 0) >= required_qty
                ):
                    return fo["id"], li["id"]

    # Pass 2: SKU fallback (for pre-feature orders without shopify_line_item_id)
    if sku_fallback:
        for fo in fulfillment_orders:
            for li in (fo.get("line_items") or []):
                if (
                    (li.get("sku") or "").strip().lower() == sku_fallback.strip().lower()
                    and int(li.get("fulfillable_quantity") or 0) >= required_qty
                ):
                    return fo["id"], li["id"]

    return None, None


# ── Tracking helpers ───────────────────────────────────────────────────────────

def _build_tracking_info(dn) -> dict:
    """
    Build the tracking_info dict for the Shopify fulfillment payload.
    Returns {} when no tracking number is present (omit from payload entirely).
    """
    number  = (dn.get("shopify_tracking_number") or "").strip()
    url     = (dn.get("shopify_tracking_url")    or "").strip()
    company = (dn.get("shopify_tracking_company") or "").strip()

    if not number:
        return {}

    info = {"number": number}
    if company and company != "Other":
        info["company"] = company
    if url:
        info["url"] = url

    return info


# ── Persistence helpers ────────────────────────────────────────────────────────

def _save_result(
    dn_name:        str,
    fulfillment_id: str  = "",
    status:         str  = "Failed",
    error:          str  = "",
):
    """
    Write fulfillment outcome fields to the Delivery Note.
    Uses db.set_value (no full document reload needed).
    """
    values = {
        "shopify_fulfillment_status": status,
        "shopify_fulfillment_error":  error[:500] if error else "",
    }
    if fulfillment_id:
        values["shopify_fulfillment_id"] = fulfillment_id

    frappe.db.set_value("Delivery Note", dn_name, values)
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — background job; must persist independently


# ── Utility ────────────────────────────────────────────────────────────────────

def _get_linked_so(dn) -> str:
    """Return the first Sales Order name linked from DN item rows, or ""."""
    for item in (dn.get("items") or []):
        if item.get("against_sales_order"):
            return item.against_sales_order
    return ""
