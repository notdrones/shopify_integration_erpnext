"""
add_activity_log_index.py

Adds a composite index on tabActivity Log (reference_doctype, reference_name, operation).

The Shopify Integration scheduler uses a correlated EXISTS subquery on this table
to determine the exact submission timestamp of Delivery Notes.  Without this index
MySQL must scan a large portion of the table for every candidate DN on each hourly
scheduler tick, which becomes slow on busy sites with millions of activity log rows.

Safe to run multiple times — skips creation when the index already exists.
"""

import frappe


def execute():
    db = frappe.db

    # Check if the index already exists to make this patch idempotent.
    existing = db.sql(
        """
        SELECT INDEX_NAME
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'tabActivity Log'
          AND INDEX_NAME   = 'idx_shopify_al_ref_op'
        LIMIT 1
        """
    )
    if existing:
        return

    db.sql(
        """
        CREATE INDEX `idx_shopify_al_ref_op`
        ON `tabActivity Log` (reference_doctype, reference_name, operation)
        """
    )
    frappe.db.commit()  # nosemgrep: frappe-manual-commit — DDL in a patch must be committed explicitly
