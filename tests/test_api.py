"""API tests: onboarding, the call lifecycle, escalation, ask list, help requests, GDPR."""

from datetime import datetime, timedelta, timezone

from conftest import ADMIN, SERVICE


def by_phone(client, phone):
    resp = client.get(f"/internal/seniors/by-phone/{phone}", headers=SERVICE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def inbound(client, senior_id):
    return client.post("/internal/calls", headers=SERVICE, json={"senior_id": senior_id}).json()["id"]


def complete(client, call_id, **body):
    body.setdefault("outcome", "answered")
    resp = client.post(f"/internal/calls/{call_id}/complete", headers=SERVICE, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def sms_to(notifier, phone):
    return [body for kind, to, body in notifier.sent if kind == "sms" and to == phone]


def test_auth_required(client):
    assert client.post("/seniors", json={}).status_code == 401
    assert client.get("/internal/seniors/by-phone/1").status_code == 401


def test_onboarding_defaults_daily_and_quiet_from_eight(client, senior):
    data = client.get(f"/seniors/{senior['id']}", headers=ADMIN).json()
    assert data["quiet_start"] == "08:00" and data["municipality"] == "Aalter"
    profile = by_phone(client, senior["phone"])
    assert profile["context"]["daily"] is True
    assert profile["next_call"] is not None


def test_fewer_days_when_family_chooses(client):
    resp = client.post("/seniors", headers=ADMIN, json={
        "display_name": "Mon", "preferred_name": "Mon", "phone": "+3250222222", "call_days": [0, 3]})
    assert by_phone(client, "+3250222222")["context"]["daily"] is False
    assert resp.status_code == 201


def test_unknown_caller_gets_404(client):
    assert client.get("/internal/seniors/by-phone/+3299", headers=SERVICE).status_code == 404


def test_first_call_flag_and_introducer(client, senior):
    ctx = by_phone(client, senior["phone"])["context"]
    assert ctx["first_call"] is True
    assert ctx["introduced_by"] == {"name": "Jan", "relation": "zoon"}
    complete(client, inbound(client, senior["id"]), summary="kennismaking", shareable_summary="Eerste babbel.")
    assert by_phone(client, senior["phone"])["context"]["first_call"] is False


def test_call_complete_stores_summary_facts_and_updates_family(client, senior, notifier):
    call_id = inbound(client, senior["id"])
    out = complete(client, call_id, summary="Over de tuin.", shareable_summary="Maria vertelde over haar tuin.",
                   facts=[{"text": "kweekt tomaten", "category": "interest"},
                          {"text": "pincode is 1234"}],
                   usage={"stt_seconds": 200, "tts_characters": 3000, "llm_input_tokens": 40000,
                          "llm_output_tokens": 1500},
                   started_at=(datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat())
    assert "family_update" in out["actions"] and out["cost_total"] > 0
    facts = client.get(f"/seniors/{senior['id']}/memory", headers=ADMIN).json()
    assert [f["text"] for f in facts] == ["kweekt tomaten"]
    assert any("haar tuin" in m for m in sms_to(notifier, "+32470000001"))


def test_urgent_flag_calls_chain_and_ack_stops_it(client, senior, notifier):
    resp = client.post("/internal/escalations", headers=SERVICE, json={
        "senior_id": senior["id"], "rule_id": "FR-30", "trigger_text": "ik ben gevallen"})
    assert resp.json()["severity"] == "urgent"
    assert ("call", "+32470000001") in [(k, t) for k, t, _ in notifier.sent]
    esc_id = resp.json()["id"]
    assert client.post(f"/escalations/{esc_id}/ack", headers=ADMIN).json()["status"] == "acknowledged"

    from sqlmodel import Session

    from babbel.api import db
    from babbel.api.main import process_due_steps
    with Session(db.get_engine()) as s:
        sent = process_due_steps(s, notifier, datetime.now(timezone.utc) + timedelta(minutes=10))
    assert sent == 0  # Lotte is not called after Jan acknowledged


def test_wellbeing_low_then_medium_when_it_grows(client, senior, notifier):
    severities = []
    for i in range(3):
        call_id = inbound(client, senior["id"])
        r = client.post("/internal/escalations", headers=SERVICE, json={
            "senior_id": senior["id"], "rule_id": "FR-37", "trigger_text": f"bang 's nachts {i}",
            "call_id": call_id})
        severities.append(r.json()["severity"])
        complete(client, call_id, summary="s", shareable_summary="Rustig gesprek.")
    assert severities == ["low", "low", "medium"]
    admin_sms = sms_to(notifier, "+32470000001")
    assert any("Opgemerkt: bang 's nachts 0" in m for m in admin_sms)  # low -> in family update
    assert any("ongerust" in m for m in admin_sms)  # medium -> its own SMS


def test_wellbeing_from_summary_is_recorded(client, senior, notifier):
    call_id = inbound(client, senior["id"])
    out = complete(client, call_id, summary="s", shareable_summary="Gezellig.",
                   wellbeing="vroeg drie keer welke dag het was")
    assert "wellbeing_signal" in out["actions"]
    assert any("vroeg drie keer" in m for m in sms_to(notifier, "+32470000001"))


def test_missed_calls_escalate(client, senior, notifier):
    from sqlmodel import Session

    from babbel.api import db
    base = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
    with Session(db.get_engine()) as s:
        ids = []
        for day in range(2):
            for attempt in (1, 2):
                c = db.Call(senior_id=senior["id"], idempotency_key=f"{senior['id']}:{day}:{attempt}",
                            slot_start=base + timedelta(days=day), attempt=attempt)
                s.add(c)
                s.flush()
                ids.append(c.id)
        s.commit()
    for cid in ids[:-1]:
        complete(client, cid, outcome="no_answer")
    out = complete(client, ids[-1], outcome="no_answer")
    assert "missed_call_sms" in out["actions"]


def test_family_ask_list_flow(client, senior, notifier):
    sid = senior["id"]
    lotte = senior["contacts"]["Lotte"]
    q1 = client.post(f"/seniors/{sid}/questions", headers=ADMIN, json={
        "text": "Ben je al bij de kapper geweest?", "added_by_contact_id": lotte}).json()
    q2 = client.post(f"/seniors/{sid}/questions", headers=ADMIN, json={
        "text": "Heb je je pillen genomen?", "kind": "check_in", "recurring": True}).json()
    assert q1["added_by_name"] == "dochter Lotte"

    ctx = by_phone(client, senior["phone"])["context"]
    assert [a["id"] for a in ctx["family_asks"]] == [q1["id"], q2["id"]]

    call_id = inbound(client, sid)
    for qid, answer in [(q1["id"], "ja, vorige donderdag"), (q2["id"], "ja, na het ontbijt")]:
        assert client.post(f"/internal/questions/{qid}/answer", headers=SERVICE,
                           json={"answer": answer, "call_id": call_id}).status_code == 200
    complete(client, call_id, summary="s", shareable_summary="Goed gesprek.")

    update = sms_to(notifier, "+32470000001")[-1]
    assert "kapper" in update and "vorige donderdag" in update and "na het ontbijt" in update

    # the one-off question is done; the recurring check-in waits until tomorrow
    assert by_phone(client, senior["phone"])["context"]["family_asks"] == []
    summaries = client.get(f"/seniors/{sid}/summaries", headers=ADMIN).json()
    assert {a["answer"] for a in summaries[0]["answers"]} == {"ja, vorige donderdag", "ja, na het ontbijt"}
    assert client.post(f"/internal/questions/{q1['id']}/answer", headers=SERVICE,
                       json={"answer": "x"}).status_code == 404


def test_unverified_contact_cannot_add_question(client, senior):
    cid = client.post(f"/seniors/{senior['id']}/contacts", headers=ADMIN, json={
        "name": "Buur", "phone": "+32470000009"}).json()["id"]
    resp = client.post(f"/seniors/{senior['id']}/questions", headers=ADMIN,
                       json={"text": "Alles goed?", "added_by_contact_id": cid})
    assert resp.status_code == 400


def test_topics_offered_once_and_activities_matched(client, senior):
    sid = senior["id"]
    client.post(f"/seniors/{sid}/topics", headers=ADMIN, json=[
        {"interest": "voetbal", "title": "Club Brugge wint", "url": "https://ex.be/a"},
        {"interest": "voetbal", "title": "Club Brugge wint", "url": "https://ex.be/a"}])
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    client.post("/activities", headers=ADMIN, json={
        "municipality": "Aalter", "title": "Tuinclub", "starts_at": soon.isoformat(), "tags": ["tuin"]})
    client.post("/activities", headers=ADMIN, json={
        "municipality": "Aalter", "title": "Gamenight", "starts_at": soon.isoformat(), "tags": ["gaming"]})
    client.post("/activities", headers=ADMIN, json={
        "municipality": "Brugge", "title": "Elders", "starts_at": soon.isoformat()})

    peek = client.get(f"/internal/seniors/{sid}/topics", headers=SERVICE).json()
    assert [t["title"] for t in peek["topics"]] == ["Club Brugge wint"]
    assert [a["title"] for a in peek["activities"]] == ["Tuinclub"]
    by_phone(client, senior["phone"])  # offers the topic
    assert client.get(f"/internal/seniors/{sid}/topics", headers=SERVICE).json()["topics"] == []


def test_help_request_goes_to_family_and_partner(client, senior, notifier, monkeypatch):
    monkeypatch.setenv("BABBEL_PARTNER_PHONE", "+32478888888")
    call_id = inbound(client, senior["id"])
    resp = client.post("/internal/help-requests", headers=SERVICE, json={
        "senior_id": senior["id"], "kind": "outing", "details": "naar de kerstmarkt in Brugge",
        "call_id": call_id})
    assert resp.status_code == 201 and resp.json()["partner"] == "Helpper"
    assert any("kerstmarkt" in m for m in sms_to(notifier, "+32470000001"))
    assert any("+32470000001" in m for m in sms_to(notifier, "+32478888888"))
    hid = resp.json()["id"]
    assert client.patch(f"/help-requests/{hid}", headers=ADMIN, json={"status": "confirmed"}).json()["status"] == "confirmed"


def test_pause_and_forget_by_voice(client, senior, notifier):
    sid = senior["id"]
    complete(client, inbound(client, sid), summary="s", facts=[{"text": "ruzie met de buurvrouw"}])
    out = client.post("/internal/memory/forget", headers=SERVICE, json={"senior_id": sid, "text": "buurvrouw"})
    assert len(out.json()["deleted"]) == 1
    client.post(f"/internal/seniors/{sid}/pause", headers=SERVICE)
    assert by_phone(client, senior["phone"])["status"] == "paused"


def test_export_and_delete(client, senior):
    sid = senior["id"]
    client.post(f"/seniors/{sid}/questions", headers=ADMIN, json={"text": "Alles goed?"})
    export = client.get(f"/seniors/{sid}/export", headers=ADMIN).json()
    assert len(export["family_questions"]) == 1 and len(export["contacts"]) == 2
    assert client.delete(f"/seniors/{sid}", headers=ADMIN).status_code == 200
    assert client.get(f"/seniors/{sid}", headers=ADMIN).status_code == 404


def test_dashboard(client, senior):
    data = client.get("/admin/dashboard", headers=ADMIN).json()
    assert data["seniors"][0]["name"] == "Maria Vermeulen"
    assert client.get("/admin?token=test-admin").status_code == 200


def test_feedback_every_third_call(client, senior):
    flags = []
    for _ in range(4):
        flags.append(by_phone(client, senior["phone"])["context"]["ask_feedback"])
        complete(client, inbound(client, senior["id"]), summary="s", shareable_summary="ok")
    assert flags == [False, False, True, False]
