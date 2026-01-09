from __future__ import annotations

from src.utilities.scan_code_concepts import (
    extract_predicates_from_js,
    extract_predicates_from_python,
)


def test_extract_predicates_from_python_skips_comments_and_docstrings():
    source = '''"""
#V#doc_predicate
"""
#V#comment_predicate
value = "#V#hasContent"
'''
    preds = extract_predicates_from_python(source, include_docstrings=False)
    assert preds == {"#V#hasContent"}

    preds_with_docs = extract_predicates_from_python(source, include_docstrings=True)
    assert "#V#doc_predicate" in preds_with_docs
    assert "#V#hasContent" in preds_with_docs


def test_extract_predicates_from_js_skips_comments():
    source = """
// #V#comment_predicate
const value = "#V#has_blob_uri";
/* #V#block_comment_predicate */
"""
    preds = extract_predicates_from_js(source)
    assert preds == {"#V#has_blob_uri"}
