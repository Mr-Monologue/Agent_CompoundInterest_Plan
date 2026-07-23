"""Normalize market-data publishers so aliases cannot fake independence."""

from __future__ import annotations

from investor_core.ledger import LedgerError

KNOWN_SOURCE_LINEAGES = {
    "ALIPAY",
    "EASTMONEY",
    "FUND_MANAGER_OFFICIAL",
    "WIND",
}


def infer_source_lineage(source_name: str, source_ref: str | None = None) -> str:
    text = f"{source_name} {source_ref or ''}".casefold()
    aliases = (
        ("EASTMONEY", ("eastmoney", "东方财富", "天天基金", "fund.eastmoney.com", "akshare")),
        ("WIND", ("wind", "万得")),
        ("ALIPAY", ("alipay", "支付宝")),
    )
    for lineage, markers in aliases:
        if any(marker in text for marker in markers):
            return lineage
    return "UNKNOWN"


def resolve_source_lineage(
    source_name: str,
    source_ref: str | None,
    supplied_lineage: str | None = None,
) -> str:
    inferred = infer_source_lineage(source_name, source_ref)
    supplied = (supplied_lineage or "").strip().upper()
    if supplied and supplied not in KNOWN_SOURCE_LINEAGES:
        raise LedgerError(
            "INVALID_SOURCE_LINEAGE",
            "source_lineage is not registered",
            details={"source_lineage": supplied},
        )
    if supplied and inferred != "UNKNOWN" and supplied != inferred:
        raise LedgerError(
            "SOURCE_LINEAGE_MISMATCH",
            "source identity conflicts with the supplied upstream lineage",
            details={"supplied_lineage": supplied, "inferred_lineage": inferred},
        )
    return supplied or inferred
