"""Opt-in isolated HTTP factory. Does not construct Core services or open a database."""

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI

from investor_core.medical_shadow_read import read_research


def create_app(packet_path: Path | None = None) -> FastAPI:
    configured = os.environ.get("INVESTOR_MEDICAL_H_PACKET")
    archive = packet_path or (Path(configured) if configured else None)
    app = FastAPI(title="Isolated R1.2 medical H research", docs_url=None, redoc_url=None)

    @app.get("/v1/medical-shadow-research")
    def read(view: Literal["SUMMARY", "DETAIL"] = "SUMMARY") -> dict[str, Any]:
        return {
            "ok": True,
            "data": read_research(archive, details=view == "DETAIL"),
            "meta": {"schema_version": "1.0", "data_quality": "WARNING"},
            "warnings": ["ISOLATED_H_ONLY_NO_FORWARD_OR_REPLACEMENT"],
        }

    return app
