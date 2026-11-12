"""Offline tests for normalizing runtime token usage into canonical counts.

Covers camelCase OpenCode, snake_case, and raw DeepSeek shapes, including the
JSON-string form the launcher returns, plus the client copy and CLI pass-through.
No network, secrets, or host names are involved.
"""

from __future__ import annotations

import json

from bundlebees_plugin.adapters import normalize_result
from bundlebees_plugin.cli import _usage
from bundlebees_plugin.client import public_result

CAMEL_JSON = json.dumps(
    {
        "cachedReadTokens": 49536,
        "inputTokens": 140,
        "outputTokens": 866,
        "thoughtTokens": 163,
        "totalTokens": 50705,
    }
)

EXTENDED_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def _usage_of(raw: dict) -> dict:
    return normalize_result(raw)["usage"]


def test_camel_case_json_string_is_parsed_without_double_counting():
    usage = _usage_of({"observed": {"usage": CAMEL_JSON}})
    assert usage["tokens"] == 50705
    assert usage["input_tokens"] == 49676
    assert usage["cache_read_input_tokens"] == 49536
    assert usage["cache_write_input_tokens"] is None
    assert usage["output_tokens"] == 1029
    assert usage["reasoning_output_tokens"] == 163


def test_camel_case_dict_is_parsed_the_same_way():
    usage = _usage_of({"usage": json.loads(CAMEL_JSON)})
    assert usage["tokens"] == 50705
    assert usage["input_tokens"] == 49676
    assert usage["cache_read_input_tokens"] == 49536
    assert usage["output_tokens"] == 1029
    assert usage["reasoning_output_tokens"] == 163


def test_snake_case_counts_include_cache_and_reasoning():
    usage = _usage_of(
        {
            "usage": {
                "input_tokens": 140,
                "cache_read_tokens": 49536,
                "cache_write_tokens": 10,
                "output_tokens": 866,
                "reasoning_tokens": 163,
                "total_tokens": 50715,
            }
        }
    )
    assert usage["tokens"] == 50715
    assert usage["input_tokens"] == 49686
    assert usage["cache_read_input_tokens"] == 49536
    assert usage["cache_write_input_tokens"] == 10
    assert usage["output_tokens"] == 1029
    assert usage["reasoning_output_tokens"] == 163


def test_snake_case_without_a_total_sums_canonical_input_and_output():
    usage = _usage_of(
        {
            "usage": {
                "input_tokens": 10,
                "cache_read_tokens": 5,
                "output_tokens": 7,
                "reasoning_tokens": 3,
            }
        }
    )
    assert usage["tokens"] == usage["input_tokens"] + usage["output_tokens"] == 25


def test_raw_deepseek_keys_are_inclusive_and_not_added_twice():
    usage = _usage_of(
        {
            "usage": {
                "prompt_tokens": 1000,
                "prompt_cache_hit_tokens": 900,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens": 50,
                "completion_tokens_details": {"reasoning_tokens": 20},
            }
        }
    )
    assert usage["tokens"] == 1050
    assert usage["input_tokens"] == 1000
    assert usage["cache_read_input_tokens"] == 900
    assert usage["cache_write_input_tokens"] is None
    assert usage["output_tokens"] == 50
    assert usage["reasoning_output_tokens"] == 20


def test_malformed_usage_string_yields_no_counts_and_no_extra_keys():
    usage = _usage_of({"usage": "{not valid json"})
    assert set(usage) == {"tokens", "cost_usd"}
    assert usage["tokens"] is None
    assert usage["cost_usd"] is None
    for key in EXTENDED_KEYS:
        assert usage.get(key) is None


def test_bools_and_negatives_are_rejected_not_coerced():
    usage = _usage_of(
        {
            "usage": {
                "totalTokens": -1,
                "inputTokens": True,
                "cachedReadTokens": -5,
                "cacheWriteTokens": False,
                "outputTokens": -7,
                "thoughtTokens": -3,
            }
        }
    )
    assert usage["tokens"] is None
    for key in EXTENDED_KEYS:
        assert usage.get(key) is None


def test_a_rejected_total_falls_back_to_known_input_and_output():
    usage = _usage_of(
        {"usage": {"totalTokens": -1, "inputTokens": 10, "outputTokens": 5}}
    )
    assert usage["tokens"] == 15
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["reasoning_output_tokens"] is None


def test_extended_keys_are_omitted_when_only_a_total_is_known():
    usage = _usage_of({"usage": {"total_tokens": 900}})
    assert usage == {"tokens": 900, "cost_usd": None}


def test_client_parses_a_string_usage_and_keeps_numbers_only():
    result = public_result(
        {
            "observed": {
                "usage": json.dumps(
                    {
                        "tokens": 50705,
                        "inputTokens": 140,
                        "model": "some-model",
                    }
                )
            }
        }
    )
    assert result["observed"]["usage"] == {"tokens": 50705, "inputTokens": 140}


def test_client_drops_a_malformed_usage_string():
    result = public_result({"observed": {"usage": "[broken"}})
    assert "usage" not in result.get("observed", {})


def test_cli_usage_passes_through_the_new_integer_keys():
    worker = {
        "usage": {
            "tokens": 50705,
            "input_tokens": 49676,
            "cache_read_input_tokens": 49536,
            "cache_write_input_tokens": 0,
            "output_tokens": 1029,
            "reasoning_output_tokens": 163,
            "cost_usd": 0.25,
            "unknown": "ignored",
        }
    }
    assert _usage(worker) == {
        "tokens": 50705,
        "input_tokens": 49676,
        "cache_read_input_tokens": 49536,
        "cache_write_input_tokens": 0,
        "output_tokens": 1029,
        "reasoning_output_tokens": 163,
        "cost_usd": 0.25,
    }


def test_cli_usage_rejects_invalid_counts_and_costs():
    worker = {
        "usage": {
            "tokens": True,
            "input_tokens": -1,
            "output_tokens": "many",
            "cost_usd": -0.5,
        }
    }
    assert _usage(worker) == {}
