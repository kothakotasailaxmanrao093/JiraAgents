"""LLM response post-processing: defensive JSON extraction (pure, no SDK)."""

from __future__ import annotations

import pytest

from review.llm_response import extract_json


def test_extract_direct_json():
    assert extract_json('{"findings": []}') == {"findings": []}


def test_extract_fenced_json():
    text = 'Here you go:\n```json\n{"findings": [1]}\n```\nthanks'
    assert extract_json(text) == {"findings": [1]}


def test_extract_wrapped_json():
    text = 'prose before {"a": 1, "b": [2,3]} prose after'
    assert extract_json(text) == {"a": 1, "b": [2, 3]}


def test_extract_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here at all")


def test_extract_raises_on_empty():
    with pytest.raises(ValueError):
        extract_json("   ")
