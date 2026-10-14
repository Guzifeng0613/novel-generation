"""Summarizer business boundary.

The summarizer owns source splitting, structured novel analysis, entity/state
merging, and summary artifacts.  It deliberately does not know about outline
approval, chapter writing, or delivery production.
"""

from .service import SUMMARIZER_STAGES, create_summary_run

__all__ = ["SUMMARIZER_STAGES", "create_summary_run"]
