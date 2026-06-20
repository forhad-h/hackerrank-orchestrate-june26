"""Tests for token_tracker.py — usage and cost tracking."""
import pytest
from modules.token_tracker import TokenTracker, PerCallMetrics, TokenSummary


@pytest.fixture
def tracker():
    t = TokenTracker()
    t.reset()
    return t


def _make_metrics(module="M3", model="openai/gpt-4o-mini", input_tokens=100,
                  output_tokens=20, cost=0.0001, latency=0.5):
    return PerCallMetrics(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_s=latency,
        cost_usd=cost,
        module=module,
        model_tier="premium",
    )


class TestRecord:
    def test_single_call(self, tracker):
        m = _make_metrics()
        tracker.record(m)
        summary = tracker.get_summary()
        assert summary.total_calls == 1
        assert summary.total_input_tokens == 100
        assert summary.total_output_tokens == 20
        assert summary.total_cost == 0.0001
        assert summary.avg_latency == 0.5

    def test_multiple_calls(self, tracker):
        tracker.record(_make_metrics(input_tokens=100, output_tokens=20, cost=0.0001, latency=0.5))
        tracker.record(_make_metrics(input_tokens=200, output_tokens=40, cost=0.0002, latency=1.0))
        s = tracker.get_summary()
        assert s.total_calls == 2
        assert s.total_input_tokens == 300
        assert s.total_output_tokens == 60
        assert s.total_cost == pytest.approx(0.0003, rel=1e-3)
        assert s.avg_latency == pytest.approx(0.75)


class TestByModel:
    def test_model_breakdown(self, tracker):
        tracker.record(_make_metrics(model="gpt-4o"))
        tracker.record(_make_metrics(model="gpt-4o"))
        tracker.record(_make_metrics(model="gpt-4o-mini"))
        s = tracker.get_summary()
        assert s.by_model["gpt-4o"] == 2
        assert s.by_model["gpt-4o-mini"] == 1


class TestByModule:
    def test_module_breakdown(self, tracker):
        tracker.record(_make_metrics(module="M3"))
        tracker.record(_make_metrics(module="M3"))
        tracker.record(_make_metrics(module="M4"))
        s = tracker.get_summary()
        assert s.by_module["M3"].total_calls == 2
        assert s.by_module["M4"].total_calls == 1

    def test_module_summary(self, tracker):
        tracker.record(_make_metrics(module="M3", input_tokens=100, cost=0.0001))
        tracker.record(_make_metrics(module="M4", input_tokens=500, cost=0.0005))
        m3 = tracker.get_module_summary("M3")
        assert m3.total_calls == 1
        assert m3.total_input_tokens == 100
        m4 = tracker.get_module_summary("M4")
        assert m4.total_calls == 1
        assert m4.total_input_tokens == 500


class TestReset:
    def test_reset_clears_state(self, tracker):
        tracker.record(_make_metrics())
        tracker.reset()
        s = tracker.get_summary()
        assert s.total_calls == 0
        assert s.total_cost == 0.0

    def test_after_reset_new_calls_count(self, tracker):
        tracker.record(_make_metrics())
        tracker.reset()
        tracker.record(_make_metrics())
        s = tracker.get_summary()
        assert s.total_calls == 1


class TestEmpty:
    def test_empty_summary(self, tracker):
        s = tracker.get_summary()
        assert s.total_calls == 0
        assert s.total_cost == 0.0
        assert s.avg_latency == 0.0
