"""Continuator business boundary.

The continuator owns memory-aware planning, outline approval, chapter writing,
review, revision and delivery.  It consumes summarizer output instead of
running source-text summary extraction.
"""

from .service import CONTINUATOR_STAGES, create_continuation_run

__all__ = ["CONTINUATOR_STAGES", "create_continuation_run"]
