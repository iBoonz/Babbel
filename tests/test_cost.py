from babbel.core.cost import Rates, Usage, compute, weekly_average_per_minute

RATES = Rates(0.015, 0.006, 0.015, 0.4, 1.6)


def test_eight_minute_call_is_under_target():
    usage = Usage(call_seconds=480, stt_seconds=240, tts_characters=3500,
                  llm_input_tokens=60_000, llm_output_tokens=2_000)
    cost = compute(usage, RATES)
    assert cost.total < 0.08 * 8  # NFR-02
    assert cost.per_minute(480) < 0.08


def test_bridge_bills_two_legs():
    one = compute(Usage(call_seconds=600), RATES).telephony
    two = compute(Usage(call_seconds=600, telephony_legs=2), RATES).telephony
    assert two == 2 * one


def test_weekly_average():
    assert weekly_average_per_minute([(0.4, 600), (0.2, 300)]) == 0.04
    assert weekly_average_per_minute([]) == 0.0
