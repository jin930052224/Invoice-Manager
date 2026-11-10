"""Buyer company grouping shared by summaries and invoice filtering."""
from datetime import date

from sqlalchemy import case, func, literal
from app.models.invoice import Invoice


def company_key_expression():
    tax_id = func.nullif(func.upper(func.trim(Invoice.buyer_tax_id)), '')
    name = func.nullif(func.trim(Invoice.buyer_name), '')
    return case(
        (tax_id.is_not(None), literal('tax:') + tax_id),
        (name.is_not(None), literal('name:') + name),
        else_=literal('unclassified'),
    )


def choose_canonical_company_names(rows):
    """Choose the most frequent name for each tax ID; break ties by recency."""
    selected = {}
    ranks = {}
    for row in rows:
        name = row["name"]
        if not name:
            continue
        rank = (
            row["occurrences"],
            row["latest_issue_date"] or date.min,
            row["latest_id"] or 0,
        )
        if rank > ranks.get(row["key"], (-1, date.min, -1)):
            selected[row["key"]] = name
            ranks[row["key"]] = rank
    return selected
