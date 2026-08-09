"""Token accounting must not count cache breakdowns twice."""

from flowly.agent.loop import _merge_turn_usage


def test_cache_tokens_are_breakdown_not_additional_context() -> None:
    total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }

    _merge_turn_usage(total, {
        "prompt_tokens": 61_444,
        "completion_tokens": 30,
        "cache_read_tokens": 61_441,
        "cache_write_tokens": 0,
    })

    assert total["prompt_tokens"] == 61_444
    assert total["cache_read_tokens"] == 61_441
    assert total["total_tokens"] == 61_474


def test_later_prompt_replaces_input_while_completion_accumulates() -> None:
    total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }

    _merge_turn_usage(total, {
        "prompt_tokens": 1_000,
        "completion_tokens": 20,
        "cache_write_tokens": 800,
    })
    _merge_turn_usage(total, {
        "prompt_tokens": 1_250,
        "completion_tokens": 15,
        "cache_read_tokens": 1_000,
        "cache_write_tokens": 50,
    })

    assert total == {
        "prompt_tokens": 1_250,
        "completion_tokens": 35,
        "total_tokens": 1_285,
        "cache_read_tokens": 1_000,
        "cache_write_tokens": 850,
    }

