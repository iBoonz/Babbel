import asyncio

from babbel.evals.run import safety_suite


def test_offline_safety_gate_passes():
    assert asyncio.run(safety_suite(use_classifier=False)) is True
