"""Deprecated — experiment_loop is now an internal service.

Use services.experiment_service instead.
"""

from __future__ import annotations

from ..services.experiment_service import (  # noqa: F401
    evaluate_ab_tests,
    run_experiment_analysis,
)

# Legacy alias for any code that imported run_experiment_loop
run_experiment_loop = run_experiment_analysis
