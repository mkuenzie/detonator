"""UI helper tests.

The _index_har_started helper was removed when captured_at was promoted to a
persisted DB column (populated during collection via map_body_files /
load_capture_manifest).  The equivalent timestamp extraction is now tested via
tests/analysis/test_har_body_map.py (captured_at propagation section).
"""

from __future__ import annotations
