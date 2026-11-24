from datetime import datetime

from babbel.core.escalation import ChainMember, build_chain, message, plan
from babbel.core.safety import SafetyFlag, Severity

CHAIN = build_chain(
    [ChainMember("Lotte", "+322", "contact", 10), ChainMember("Jan", "+321", "admin"),
     ChainMember("Thuiszorg", "+323", "care_partner"), ChainMember("Oud", "+324", "contact", opted_out=True)],
    operator_phone="+329",
)


def test_chain_order():
    assert [m.name for m in CHAIN] == ["Jan", "Lotte", "Thuiszorg", "Operator"]


def test_urgent_walks_the_chain():
    steps = plan(SafetyFlag("FR-30", Severity.URGENT, "gevallen", "keyword"), CHAIN, 3)
    assert [(s.offset_minutes, s.member.name, s.channels) for s in steps][:2] == [
        (0, "Jan", ("call", "sms")), (3, "Lotte", ("call", "sms"))]


def test_medium_one_sms_low_nothing():
    medium = plan(SafetyFlag("FR-37", Severity.MEDIUM, "bang", "agent_tool"), CHAIN)
    assert [(s.member.name, s.channels) for s in medium] == [("Jan", ("sms",))]
    assert plan(SafetyFlag("FR-37", Severity.LOW, "bang", "agent_tool"), CHAIN) == []


def test_messages_are_filled():
    at = datetime(2026, 11, 24, 10, 41)
    msg = message(SafetyFlag("FR-32", Severity.CRITICAL, "wil niet meer", "keyword"), senior_name="Maria", at=at)
    assert "Maria" in msg and "10:41" in msg and "1813" in msg
    msg37 = message(SafetyFlag("FR-37", Severity.MEDIUM, "bang 's nachts", "keyword"), senior_name="Maria", at=at)
    assert "bang 's nachts" in msg37
