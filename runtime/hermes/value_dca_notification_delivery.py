"""Drain the Core notification outbox through the official Hermes send command."""

from __future__ import annotations

import os
import sys

from _value_dca_runner import _request

from investor_core.hermes_delivery import resolve_delivery_target, send_with_hermes
from investor_core.ledger import LedgerError


def main() -> int:
    profile = os.environ.get("INVESTOR_HERMES_PROFILE", "investor").strip() or "investor"
    try:
        claimed = _request("/v1/notification-deliveries/claim?limit=10")
        for item in list(claimed.get("items", [])):
            target = ""
            try:
                target = resolve_delivery_target(
                    str(item["delivery_target"]),
                    profile=profile,
                )
                evidence = send_with_hermes(
                    profile=profile,
                    target=target,
                    message=str(item["payload"]["display_text"]),
                )
                outcome = "DELIVERED"
                error_code = None
                provider_message_id = f"hermes-send:{item['attempt_id']}"
            except LedgerError as exc:
                evidence = {
                    "adapter": "value-dca-hermes-send",
                    "adapter_version": "1",
                    "evidence_level": "DELIVERY_FAILED",
                    "target": target or str(item["delivery_target"]),
                    "error_details": exc.details,
                }
                outcome = "FAILED"
                error_code = exc.code
                provider_message_id = None
            _request(
                "/v1/notification-deliveries/receipt",
                {
                    "outbox_id": item["outbox_id"],
                    "attempt_id": item["attempt_id"],
                    "receipt_token": item["receipt_token"],
                    "outcome": outcome,
                    "provider": "HERMES_SEND",
                    "provider_message_id": provider_message_id,
                    "evidence": evidence,
                    "error_code": error_code,
                    "actor_ref": "hermes-delivery-adapter",
                },
            )
        # Empty stdout is intentional: the adapter itself must never be redelivered.
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
