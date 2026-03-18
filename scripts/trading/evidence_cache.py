"""Cache manager for precomputed simulation evidence and outcomes."""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from scripts.tools.json_cache import read_cached_json


class EvidenceTracker:
    """Manages loading and caching of JSON evidence files."""

    def __init__(self) -> None:
        self.signal_evidence_path = Path(
            os.getenv("SIGNAL_EVIDENCE_PATH", "docs/evidence/outcome_weekly_costs.json")
        )
        self._signal_evidence_cache: Optional[Dict[str, Any]] = None
        self._signal_evidence_mtime: Optional[float] = None

        self.roc_summary_path = Path(
            os.getenv(
                "ROC_REGIME_SUMMARY_PATH", "docs/evidence/roc_regime_summary.json"
            )
        )
        self._roc_summary_cache: Optional[Dict[str, Any]] = None
        self._roc_summary_mtime: Optional[float] = None

    def get_signal_outcomes(self) -> Dict[str, Any]:
        return read_cached_json(  # type: ignore
            self,
            self.signal_evidence_path,
            "_signal_evidence_cache",
            "_signal_evidence_mtime",
            "evidence",
        )

    def get_regime_roc_summary(self) -> Dict[str, Any]:
        return read_cached_json(  # type: ignore
            self,
            self.roc_summary_path,
            "_roc_summary_cache",
            "_roc_summary_mtime",
            "roc_summary",
        )
