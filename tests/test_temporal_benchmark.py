import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.run_temporal import materialize  # noqa: E402
from xangi_search.index import SearchIndex  # noqa: E402


def test_temporal_fixture_preserves_age_and_content(tmp_path: Path):
    corpus = tmp_path / "corpus"
    manifest, timestamps = materialize(corpus)
    index = SearchIndex(corpus)
    try:
        with patch(
            "xangi_search.index.time",
            SimpleNamespace(time=lambda: timestamps["reference_epoch"]),
        ):
            for row in manifest["files"]:
                path = row["path"]
                age = timestamps["age_days"][path]
                assert (corpus / path).stat().st_mtime == timestamps[
                    "reference_epoch"
                ] - age * 86400
                assert index._freshness(path) == pytest.approx(max(0.5, 1 - age / 30))
    finally:
        index.close()
