"""
item.py — SKU-based item matching and rate calculation for Shopify line items.

Core Rule
─────────
Tax rate is ALWAYS read from ERPNext Item Tax Template.
Never read or trust the tax rate from Shopify.

Calculation spec (item-wise, GST-inclusive prices)
───────────────────────────────────────────────────
Shopify ships tax-inclusive prices (for IN / EU stores with
`taxes_included = true`).  ERPNext stores tax-exclusive `rate`
and applies tax on top via the Sales Taxes & Charges table.

So for every Shopify line we must:

  net_incl_gst   = (unit_price × qty) − line_discount
  rate_excl_gst  = net_incl_gst / (1 + tax_rate / 100)
  rate_per_unit  = round(rate_excl_gst / qty, 2)

Rate is rounded to 2 decimals because ERPNext stores it that way.
The cumulative paise-level rounding error is absorbed by the
last row (`_adjust_rows_for_rounding`) so that the ERPNext
grand_total matches Shopify's `total_price` to the paisa.

Edge cases handled:
    • Items with different GST rates (0 / 3 / 5 / 12 / 18 / 28 %)
    • Multiple quantities
    • Free items (100 % discount)
    • Mixed discounts: line-level, order-level, none
    • Shipping as a tax-bearing row
    • Paise-level rounding drift → absorbed by the last non-zero row
"""

import frappe
from frappe.utils import flt


# ── Public API ─────────────────────────────────────────────────────────────────

def map_line_items(
    shopify_line_items: list,
    settings,
    order_discount: float = 0.0,
    shopify_items_total: float = 0.0,
) -> list:
    """
    Convert Shopify line_items to ERPNext Sales Order item dicts
    (tax-exclusive rates, ready for the SO items child table).

    NOTE: this function returns items only.  The caller is responsible for
    appending the shipping row and calling `adjust_rows_to_match_total()`
    once all rows (items + shipping) are assembled, so the rounding
    absorber sees the full set of rows.

    :param shopify_line_items:  order["line_items"] from Shopify webhook
    :param settings:            Shopify Settings document
    :param order_discount:      order["total_discounts"] (used to fill gaps
                                not already distributed into each line)
    :param shopify_items_total: Shopify total_price minus shipping — kept
                                for backward compatibility; not used for
                                rounding any more.  Rounding now happens
                                at the SO level after the shipping row is
                                added.  See `adjust_rows_to_match_total`.
    :returns: list of item dicts ready for SO items table.
    """
    if not shopify_line_items:
        return []

    # ── Pass 1: resolve ERPNext items and collect raw Shopify data ─────────────
    raw = []
    for line in shopify_line_items:
        sku   = (line.get("sku") or "").strip()
        title = line.get("title") or line.get("name") or sku or "Unknown Item"

        if not sku:
            frappe.throw(
                f"Shopify line item '{title}' has no SKU. "
                "Add a SKU in Shopify that matches an ERPNext Item Code.",
                frappe.ValidationError
            )

        item = get_item_and_tax(sku, settings.company)
        if not item:
            frappe.throw(
                f"No ERPNext item found for Shopify SKU '{sku}' (Product: '{title}'). "
                "Ensure the SKU in Shopify matches the Item Code in ERPNext exactly.",
                frappe.ValidationError
            )

        qty           = flt(line.get("quantity") or 1)
        unit_price    = flt(line.get("price") or 0)            # GST-inclusive per unit
        line_discount = flt(line.get("total_discount") or 0)   # already allocated by Shopify
        item_total    = unit_price * qty                        # GST-inclusive line total

        raw.append({
            "title":          title,
            "qty":            qty,
            "unit_price":     unit_price,
            "item_total":     item_total,
            "line_discount":  line_discount,
            "item":           item,
        })

    # ── Distribute undistributed order-level discount ─────────────────────────
    # Shopify usually distributes order-level discounts into each line's
    # `total_discount` field already.  Anything left over is spread
    # proportionally across rows that have no line-level discount.
    total_before_discount = sum(r["item_total"] for r in raw)
    sum_line_discounts    = sum(r["line_discount"] for r in raw)
    undistributed         = max(0.0, flt(order_discount) - sum_line_discounts)

    # Denominator: items that don't already carry a line-level discount.
    undiscounted_total = sum(
        r["item_total"] for r in raw if r["line_discount"] == 0
    ) or total_before_discount  # fallback guards against divide-by-zero

    # Distribute undistributed discount in integer-paise units so the
    # sum of allocated discounts exactly equals `undistributed`.
    allocated = []
    running_share = 0.0
    remaining = undistributed
    candidates = [i for i, r in enumerate(raw) if r["line_discount"] == 0]
    for idx, i in enumerate(candidates):
        r = raw[i]
        if idx == len(candidates) - 1:
            # last candidate gets whatever is left so we don't drop a paisa
            share = remaining
        else:
            share = round(undistributed * (r["item_total"] / undiscounted_total), 2)
            share = min(share, remaining)
        allocated.append((i, share))
        remaining = round(remaining - share, 2)

    # Materialise the per-line item_discount
    undistributed_map = {i: share for i, share in allocated}

    # ── Pass 2: calculate rate per item ───────────────────────────────────────
    so_items = []
    for idx, r in enumerate(raw):
        qty           = r["qty"]
        item_total    = r["item_total"]
        line_discount = r["line_discount"]
        item          = r["item"]
        tax_rate      = flt(item["tax_rate"])

        if line_discount > 0:
            item_discount = line_discount
        else:
            item_discount = undistributed_map.get(idx, 0.0)

        # GST-inclusive net (what the customer paid for this line)
        net_including_gst = item_total - item_discount

        if net_including_gst <= 0 or qty <= 0:
            rate = 0.0
        else:
            if tax_rate > 0:
                rate_excl_gst = net_including_gst / (1.0 + tax_rate / 100.0)
            else:
                rate_excl_gst = net_including_gst
            rate = round(rate_excl_gst / qty, 2)

        # Base unit price (tax exclusive)
        if tax_rate > 0:
            base_rate_excl = unit_price / (1.0 + tax_rate / 100.0)
        else:
            base_rate_excl = unit_price
        
        price_list_rate = round(base_rate_excl, 2)

        so_items.append({
            "item_code": item["item_code"],
            "item_name": item["item_name"],
            "qty":       qty,
            "rate":      rate,
            "price_list_rate": price_list_rate,
            "uom":       item["uom"],
            # Internal keys (stripped / used in sales_order.py)
            "_tax_template":      item["tax_template"],
            "_tax_rate":          tax_rate,
            "_net_including_gst": net_including_gst,
        })

    return so_items


# ── Rounding reconciliation (items + shipping together) ───────────────────────

def adjust_rows_to_match_total(so_rows: list, target_inclusive_total: float):
    """
    Mutate `so_rows` (list of dicts that each have `rate`, `qty`, `_tax_rate`)
    so that the sum of GST-inclusive line totals, as ERPNext would compute
    them, equals `target_inclusive_total` exactly.

    ERPNext formula per row:
        amount  = round(rate * qty, 2)
        tax     = round(amount * tax_rate / 100, 2)
        total   = amount + tax

    Because each row rounds twice (amount and tax), a single divide-by
    (1 + tax%) can land on a rate that rounds back to the same value,
    producing a paisa of drift that cannot be killed from one row alone.
    Strategy:

      1. Stage 1 — single-row search.  For each row, try rates in a
         ±MAX_PAISE_PER_ROW window around the original; keep the
         combination that minimises |grand_total − target|.
      2. Stage 2 — two-row search.  If stage 1 cannot hit zero (because
         two rows share the same tax rate and neither alone can nudge
         the total by 1 paisa), try pairs of rows each within ±3 paise
         of their original rate.
      3. Reject adjustments beyond MAX_PAISE_PER_ROW on any one row —
         if Shopify's total_price is that far off, the data is bad and
         we preserve original rates + log a warning.
    """
    if not so_rows or target_inclusive_total <= 0:
        return

    target = round(flt(target_inclusive_total), 2)
    original_rates = [flt(r.get("rate")) for r in so_rows]

    def calc_total():
        return round(sum(_erpnext_row_inclusive(r) for r in so_rows), 2)

    def restore():
        for r, orig in zip(so_rows, original_rates):
            r["rate"] = orig

    if round(calc_total() - target, 2) == 0:
        return

    # Safety: we only ever want to nudge rates by a small paise-level amount.
    # If Shopify's total_price disagrees with the sum of (price × qty − discount)
    # by more than this, we log and accept the mismatch rather than mangling
    # the shipping or an item row.
    MAX_PAISE_PER_ROW = 10  # ±₹0.10 around each row's original rate

    # ── Stage 1: single-row absorber ──────────────────────────────────────────
    # Works in the common case (Widget-only, different GST rates, etc.) and is
    # cheap enough to always run first.
    best = None  # (abs_diff, [(row_index, new_rate), ...])

    for i in range(len(so_rows) - 1, -1, -1):
        row = so_rows[i]
        qty = flt(row.get("qty"))
        if qty <= 0 or original_rates[i] <= 0:
            continue

        restore()

        # Only try rates near the original — never re-seed from `remaining`,
        # which could push a row far away (e.g. shipping → ₹0.01).
        for delta_paise in range(-MAX_PAISE_PER_ROW, MAX_PAISE_PER_ROW + 1):
            cand = round(original_rates[i] + delta_paise / 100.0, 2)
            if cand <= 0:
                continue
            row["rate"] = cand
            diff = abs(round(calc_total() - target, 2))
            if best is None or diff < best[0]:
                best = (diff, [(i, cand)])
            if diff == 0:
                break
        if best and best[0] == 0:
            break

    # ── Stage 2: 2-row absorber (only if stage 1 could not hit zero) ──────────
    # Handles the case where two rows share the same tax rate and neither
    # can alone shift the total by 1 paisa — but together (one up, one down)
    # they can.  Iterates a ±3 paise window around each row's original rate.
    if best is None or best[0] != 0:
        row_candidates = []
        for i, row in enumerate(so_rows):
            qty = flt(row.get("qty"))
            orig = original_rates[i]
            if qty <= 0 or orig <= 0:
                row_candidates.append([])
                continue
            row_candidates.append(
                [round(orig + d / 100.0, 2) for d in range(-3, 4) if orig + d / 100.0 > 0]
            )

        for i in range(len(so_rows)):
            if not row_candidates[i]:
                continue
            for j in range(i + 1, len(so_rows)):
                if not row_candidates[j]:
                    continue
                for ri in row_candidates[i]:
                    so_rows[i]["rate"] = ri
                    for rj in row_candidates[j]:
                        so_rows[j]["rate"] = rj
                        # Keep every other row at its original rate
                        for k in range(len(so_rows)):
                            if k != i and k != j:
                                so_rows[k]["rate"] = original_rates[k]
                        diff = abs(round(calc_total() - target, 2))
                        if best is None or diff < best[0]:
                            best = (diff, [(i, ri), (j, rj)])
                        if diff == 0:
                            break
                    if best and best[0] == 0:
                        break
                if best and best[0] == 0:
                    break
            if best and best[0] == 0:
                break

    # Apply the winning combination; reset everything else to original.
    restore()
    if best is not None:
        for idx, new_rate in best[1]:
            so_rows[idx]["rate"] = new_rate

    # Log at DEBUG — rounding adjustments are expected normal behaviour
    # (every Shopify order with GST will hit this path).  Using log_error()
    # here would flood the ERPNext Error Log with non-error entries.
    try:
        frappe.logger("shopify_integration").debug(
            f"Shopify rounding absorbed: target={target}, "
            f"final_calc={calc_total():.2f}, "
            f"adjustments={best[1] if best else None}, "
            f"residual_diff={best[0] if best else None}"
        )
    except Exception:
        pass


def _erpnext_row_inclusive(row: dict) -> float:
    """
    Simulate ERPNext's per-row (amount + tax) the way validate() computes it.

    When `_split_tax` is True, India Compliance has applied CGST + SGST as two
    separate rows each at half the effective rate.  ERPNext rounds each row
    independently, so we must simulate both halves to match the actual total.

    Example — 5% GST on ₹12,380.95:
        Single IGST:   round(12380.95 × 0.05, 2) = 619.05  → total 12999.00
        Split CGST+SGST: round(12380.95 × 0.025, 2) × 2
                        = 309.52 × 2 = 619.04 → total 12999.99  (₹0.01 diff)
    """
    rate      = flt(row.get("rate"))
    qty       = flt(row.get("qty"))
    tax_rate  = flt(row.get("_tax_rate") or 0)
    split_tax = bool(row.get("_split_tax"))
    amount    = round(rate * qty, 2)
    if split_tax and tax_rate > 0:
        half = tax_rate / 2.0
        tax  = round(amount * half / 100.0, 2) + round(amount * half / 100.0, 2)
    else:
        tax = round(amount * tax_rate / 100.0, 2)
    return amount + tax


# ── ERPNext item / tax helpers ─────────────────────────────────────────────────

def get_item_and_tax(sku: str, company: str) -> dict:
    """
    Find an ERPNext Item by SKU (item_code).
    Also fetches the Item Tax Template and tax rate.

    Returns dict: {item_code, item_name, uom, tax_template, tax_rate}
    or None if item is missing.  Raises if item found but has no tax template.
    """
    if not sku:
        return None

    item_name = frappe.db.get_value("Item", {"item_code": sku, "disabled": 0}, "name")
    if not item_name:
        return None

    item_doc = frappe.get_doc("Item", item_name)

    tax_template, tax_rate = _get_item_tax_template(item_doc, company)

    if not tax_template:
        frappe.throw(
            f"Item '{sku}' ({item_doc.item_name}) has no Item Tax Template configured. "
            "Add an Item Tax Template in the item master's Taxes table before "
            "processing Shopify orders.",
            frappe.ValidationError
        )

    return {
        "item_code":    item_doc.name,
        "item_name":    item_doc.item_name,
        "uom":          item_doc.sales_uom or item_doc.stock_uom or "Nos",
        "tax_template": tax_template,
        "tax_rate":     flt(tax_rate),
    }


def _get_item_tax_template(item_doc, company: str):
    """
    Get best Item Tax Template from item's taxes child table, and compute
    the *effective* GST rate that India Compliance will apply on the Sales
    Order.

    Priority for template pick:
    1. First row whose template belongs to the matching company
    2. First row (any company) as fallback

    Effective rate is derived by inspecting every row of the Item Tax Template
    Detail and bucketing by account name:

        • CGST / SGST / UTGST rows → intra-state bucket
        • IGST rows               → inter-state bucket
        • Anything else           → raw bucket (fallback)

    Because India Compliance applies EITHER the intra-state bucket
    (CGST + SGST) OR the inter-state bucket (IGST) — never both — the
    effective rate is `max(intra_total, inter_total)`.  If a template
    has neither (e.g. a non-GST tax), we use the raw sum so we still
    back-calculate against something sensible.

    Returns (template_name, effective_tax_rate) or ("", 0).
    """
    if not item_doc.get("taxes"):
        return "", 0

    company_match = None
    first_row     = None

    for row in item_doc.taxes:
        tmpl_name = row.get("item_tax_template")
        if not tmpl_name:
            continue
        if first_row is None:
            first_row = tmpl_name
        tmpl_company = frappe.db.get_value("Item Tax Template", tmpl_name, "company")
        if tmpl_company == company:
            company_match = tmpl_name
            break

    chosen = company_match or first_row
    if not chosen:
        return "", 0

    rows = frappe.get_all(
        "Item Tax Template Detail",
        filters={"parent": chosen},
        fields=["tax_rate", "tax_type"],
        order_by="idx asc",
    )

    intra_total = 0.0   # CGST + SGST + UTGST
    inter_total = 0.0   # IGST
    raw_total   = 0.0

    for r in rows:
        rate    = flt(r.get("tax_rate", 0))
        account = (r.get("tax_type") or "").upper()

        # Only consider positive-rate Output Tax rows.
        # India Compliance Item Tax Templates often contain RCM rows
        # (e.g. "Output Tax IGST RCM") and Input Tax rows (e.g. "Input
        # Tax IGST") in addition to the standard Output Tax rows.
        # IC never applies RCM or Input Tax rows on a normal sales
        # transaction, so including them would inflate the effective
        # rate (e.g. 5% GST → 10% if Output+Input are both counted).
        if rate <= 0 or "RCM" in account or "INPUT" in account:
            continue

        raw_total += rate
        if "IGST" in account:
            inter_total += rate
        elif "CGST" in account or "SGST" in account or "UTGST" in account:
            intra_total += rate

    effective = max(intra_total, inter_total)
    if effective == 0:
        effective = raw_total

    return chosen, effective
