"""R1.2 provenance time contract. Unknown is not an invented publication instant."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import model_validator

from investor_core.r11_inputs import Context, Source


class ResearchSource(Source):
    first_retrieved_at: datetime | None = None  # type: ignore[assignment]
    published_at: datetime | None = None  # type: ignore[assignment]
    publication_precision: Literal["INSTANT", "DATE", "UNKNOWN"] = "INSTANT"  # type: ignore[assignment]
    publication_timezone: str | None = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def aware(self) -> "ResearchSource":
        if self.first_retrieved_at is not None and self.first_retrieved_at.tzinfo is None:
            raise ValueError("actual retrieval timezone required")
        if self.published_at is None:
            if self.publication_precision != "UNKNOWN" or self.publication_timezone is not None:
                raise ValueError("unknown publication must stay unknown")
        elif self.publication_precision == "UNKNOWN" or not self.publication_timezone:
            raise ValueError("publication precision and timezone required")
        else:
            if self.published_at.tzinfo is None:
                raise ValueError("publication timezone required")
            ZoneInfo(self.publication_timezone)
        return self

    def known_at(self) -> datetime:
        if self.published_at is None or self.first_retrieved_at is None:
            raise ValueError("PUBLICATION_TIME_UNKNOWN")
        return super().known_at()


class ResearchContext(Context):
    sources: dict[str, ResearchSource]  # type: ignore[assignment]

    def source_gaps(self, key: str | None, *, account_ref: str | None = None) -> list[str]:
        src = self.sources.get(key or "")
        if (
            src is not None
            and (src.published_at is None or src.first_retrieved_at is None)
            and self.evidence_class != "H"
        ):
            return ["PUBLICATION_OR_RETRIEVAL_TIME_UNKNOWN"] + (
                ["SOURCE_" + src.quality] if src.quality != "OFFICIAL" else []
            )
        return super().source_gaps(key, account_ref=account_ref)
