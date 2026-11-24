"""Test setup: a fresh SQLite database per test and a console notifier we can inspect."""

import os

os.environ.setdefault("BABBEL_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("BABBEL_SERVICE_TOKEN", "test-service")
os.environ.setdefault("BABBEL_NOTIFIER", "console")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import SQLModel, create_engine  # noqa: E402

from babbel.api import db  # noqa: E402
from babbel.api.main import app  # noqa: E402
from babbel.core.notify import ConsoleNotifier  # noqa: E402

ADMIN = {"X-Admin-Token": "test-admin"}
SERVICE = {"X-Service-Token": "test-service"}


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    db._engine = eng
    yield eng
    db._engine = None


@pytest.fixture()
def notifier():
    n = ConsoleNotifier()
    app.state.notifier = n
    return n


@pytest.fixture()
def client(engine, notifier):
    return TestClient(app)


@pytest.fixture()
def senior(client):
    """Maria, with a verified family admin (her son Jan) and a verified daughter Lotte."""
    resp = client.post("/seniors", headers=ADMIN, json={
        "display_name": "Maria Vermeulen", "preferred_name": "Maria", "phone": "+3250111111",
        "municipality": "Aalter", "family": ["zoon Jan", "dochter Lotte"],
        "interests": ["voetbal", "tuin"],
    })
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    contacts = {}
    for name, relation, role, phone in [("Jan", "zoon", "admin", "+32470000001"),
                                        ("Lotte", "dochter", "contact", "+32470000002")]:
        cid = client.post(f"/seniors/{sid}/contacts", headers=ADMIN, json={
            "name": name, "relation": relation, "role": role, "phone": phone}).json()["id"]
        code = db_contact_code(cid)
        assert client.post(f"/contacts/{cid}/verify", headers=ADMIN, json={"code": code}).status_code == 200
        contacts[name] = cid
    return {"id": sid, "phone": "+3250111111", "contacts": contacts}


def db_contact_code(contact_id: int) -> str:
    from sqlmodel import Session

    with Session(db.get_engine()) as s:
        return s.get(db.Contact, contact_id).verification_code
