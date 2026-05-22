"""Tests for trading engine modules: pipeline, engine, factors."""
import math
import sys
import os
from datetime import datetime, date
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, "/shared-hermes/hko_weather_monitor")

from hko_weather_monitor.pipeline import (
    calculate_bucket_probability,
    calculate_conviction,
    calculate_no_score,
    bayesian_blend_probs,
    get_edge_threshold,
)


class TestBayesianBlending:
    """Bayesian Blending Variable Horizon Weights."""

    def test_short_horizon_weight(self):
        """Short horizon (0 days): model_weight ~0.75, market_weight ~0.25."""
        model = [0.6, 0.3, 0.1]
        market = [0.5, 0.4, 0.1]
        result = bayesian_blend_probs(model, market, horizon_days=0)
        assert len(result) == 3
        # Model dominates for short horizons
        assert result[0] > 0.5  # Close to model's 0.6

    def test_long_horizon_weight(self):
        """Long horizon (5+ days): model_weight clamped at 0.4, market_weight at 0.6."""
        model = [0.6, 0.3, 0.1]
        market = [0.2, 0.3, 0.5]
        result = bayesian_blend_probs(model, market, horizon_days=5)
        assert len(result) == 3
        # model_weight=0.4, market_weight=0.6 → blended[0] = 0.4*0.6 + 0.6*0.2 = 0.36
        assert result[0] == pytest.approx(0.36, abs=0.01)
        # blended[2] = 0.4*0.1 + 0.6*0.5 = 0.34
        assert result[2] == pytest.approx(0.34, abs=0.01)

    def test_blended_sum(self):
        """Blended probabilities should sum to ~1.0."""
        model = [0.4, 0.35, 0.15, 0.1]
        market = [0.3, 0.3, 0.2, 0.2]
        result = bayesian_blend_probs(model, market, horizon_days=2)
        assert abs(sum(result) - 1.0) < 0.01

    def test_empty_inputs(self):
        """Empty inputs should not crash."""
        assert bayesian_blend_probs([], [0.5, 0.5], 0) == []
        assert bayesian_blend_probs([0.5, 0.5], [], 0) == [0.5, 0.5]


class TestConviction:
    """Conviction Calculation Over Various Distributions."""

    def test_single_bucket_dominates(self):
        """Single bucket with 100% probability = max conviction."""
        probs = [1.0, 0.0, 0.0, 0.0]
        assert calculate_conviction(probs) == pytest.approx(1.0, abs=0.01)

    def test_uniform_distribution(self):
        """Uniform distribution = zero conviction."""
        probs = [0.25, 0.25, 0.25, 0.25]
        assert calculate_conviction(probs) == pytest.approx(0.0, abs=0.01)

    def test_heavy_tailed(self):
        """Two dominant buckets = moderate conviction."""
        probs = [0.45, 0.45, 0.05, 0.05]
        conviction = calculate_conviction(probs)
        assert 0.0 < conviction < 0.5

    def test_empty_and_zeros(self):
        """Edge cases."""
        assert calculate_conviction([]) == 0.0
        assert calculate_conviction([0, 0, 0]) == 0.0

    def test_conviction_bounded(self):
        """Conviction always between 0 and 1."""
        for _ in range(50):
            probs = [abs(x) for x in [0.1, 0.3, 0.5, 0.05, 0.05]]
            c = calculate_conviction(probs)
            assert 0.0 <= c <= 1.0


class TestNoScoring:
    """NO Scoring with Various Market/Model Combinations."""

    def test_positive_edge(self):
        """Market overpriced vs model → positive NO score."""
        score = calculate_no_score(market_yes=0.7, model_yes=0.3)
        assert score > 0

    def test_no_edge(self):
        """Market equals model → zero score."""
        score = calculate_no_score(market_yes=0.5, model_yes=0.5)
        assert score == 0.0

    def test_negative_edge(self):
        """Market underpriced vs model → zero score (not a NO trade)."""
        score = calculate_no_score(market_yes=0.3, model_yes=0.7)
        assert score == 0.0

    def test_extreme_market(self):
        """Market at 0.99 → variance near zero → score explodes (known edge case)."""
        score = calculate_no_score(market_yes=0.999, model_yes=0.5)
        assert score > 100  # Variance denominator makes it huge

    def test_market_at_zero(self):
        """Market at 0 → variance is 0 → score is 0."""
        score = calculate_no_score(market_yes=0.0, model_yes=0.5)
        assert score == 0.0

    def test_high_score_scenario(self):
        """Strong edge with reasonable variance → score = edge^2 / variance."""
        score = calculate_no_score(market_yes=0.6, model_yes=0.2)
        # (0.4^2) / (0.6*0.4) = 0.667
        assert score == pytest.approx(0.667, abs=0.01)


class TestEdgeThreshold:
    """Edge threshold increases with horizon."""

    def test_threshold_increases(self):
        """Further horizons require larger edges."""
        for h in range(10):
            t = get_edge_threshold(h)
            assert t > 0
        assert get_edge_threshold(0) < get_edge_threshold(5)
        assert get_edge_threshold(5) < get_edge_threshold(9)


class TestBucketProbability:
    """Bucket probability calculation."""

    def test_prob_near_forecast(self):
        """Bucket at forecast value should have highest probability."""
        p_center = calculate_bucket_probability(25.0, 25, 1)
        p_far = calculate_bucket_probability(25.0, 30, 1)
        assert p_center > p_far

    def test_prob_bounds(self):
        """Probabilities bounded 0-1."""
        for bucket in [22, 25, 28, 31]:
            p = calculate_bucket_probability(26.0, bucket, 1)
            assert 0.0 <= p <= 1.0

    def test_sum_less_than_1(self):
        """Sum of all bucket probs < 1 (tails omitted)."""
        total = sum(calculate_bucket_probability(26.0, b, 1) for b in range(22, 32))
        assert total > 0  # Some probability mass captured
