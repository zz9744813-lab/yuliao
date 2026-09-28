"""Unknown registry labels stay out of human evidence on every path."""
from __future__ import annotations

import pytest

from app import knowledge_query as KQ
from app.source_policy import compliant_human_source
from scripts import k2_extract_backfill as K2


@pytest.mark.parametrize("source_type,admitted", [
    ("human_fiction", True),
    ("production_nonbenchmark_k2v2", True),
    ("production_nonbenchmark_", True),
    ("unverified_corpus", False),
    ("fixture", False),
    ("synthetic", False),
    ("commentary", False),
    (None, False),
])
def test_k2_k3_share_explicit_human_source_floor(source_type, admitted):
    assert compliant_human_source(source_type) is admitted
    assert KQ.compliant_human_source(source_type) is admitted
    assert K2.nonbenchmark_compliant_source(source_type) is admitted
