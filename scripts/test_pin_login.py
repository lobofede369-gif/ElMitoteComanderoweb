"""Regression: keypad login, PIN uniqueness, owner activation and staff permissions.
Uses a disposable SQLite file, not the restaurant's PostgreSQL database.
"""
import os
import tempfile
from pathlib import Path
from uuid import uuid4

dbfile=Path(tempfile.gettempdir())/("mitote-pin-test-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(dbfile)
os.environ["SECRET_KEY"]="unit-test-only-pin-pepper-2026"
os.environ["ADMIN_USERNAME"]="propietario_test"
os.environ["ADMIN_PASSWORD"]="LegacyOwner2026!_test"
os.environ.pop("FIREBASE_SERVICE_ACCOUNT_JSON",None)

import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    legacy=client.post("/api/login",json={"username":"propietario_test",
                                           "secret":"LegacyOwner2026!_test"})
    assert legacy.status_code==200,legacy.text
    owner_token=legacy.json()["token"]
    def auth(token):
        return {"Authorization":"Bearer "+token}
    users=client.get("/api/users",headers=auth(owner_token))
    assert users.status_code==200,users.text
    owner=next(u for u in users.json() if u["role"]=="owner")
    assert not owner["has_pin"]
    missing=client.post("/api/staff",json={"name":"Mesero Uno","role":"waiter","pin":"624018"})
    assert missing.status_code==401,missing.text

    create=client.post("/api/staff",headers=auth(owner_token),
       json={"name":"Mesero Uno","role":"waiter","pin":"624018"})
    assert create.status_code==200,create.text
    waiter_id=create.json()["id"]
    keypad=client.post("/api/login/pin",json={"pin":"624018"})
    assert keypad.status_code==200,keypad.text
    assert keypad.json()["user"]["role"]=="waiter"
    waiter_token=keypad.json()["token"]
    # No employee can create another access, and no duplicate PIN is accepted.
    denied=client.post("/api/staff",headers=auth(waiter_token),
       json={"name":"Otro empleado","role":"bar","pin":"736249"})
    assert denied.status_code==403,denied.text
    duplicate=client.post("/api/staff",headers=auth(owner_token),
       json={"name":"Otra mesera","role":"waiter","pin":"624018"})
    assert duplicate.status_code==409,duplicate.text

    # Wrong or malformed PINs fail without revealing employee identities.
    wrong=client.post("/api/login/pin",json={"pin":"000000"})
    assert wrong.status_code==401,wrong.text
    bad=client.post("/api/login/pin",json={"pin":"1234"})
    assert bad.status_code==422,bad.text
    letters=client.post("/api/login/pin",json={"pin":"12AB56"})
    assert letters.status_code==422,letters.text
    leading=client.post("/api/staff",headers=auth(owner_token),
       json={"name":"Caja Dos","role":"cashier","pin":"002468"})
    assert leading.status_code==200,leading.text
    cashier=client.post("/api/login/pin",json={"pin":"002468"})
    assert cashier.status_code==200 and cashier.json()["user"]["role"]=="cashier"

    # Existing accounts keep their data and can be assigned PINs by owner.
    reset=client.patch(f"/api/staff/{waiter_id}/pin",headers=auth(owner_token),
       json={"pin":"813642"})
    assert reset.status_code==200,reset.text
    old=client.post("/api/login/pin",json={"pin":"624018"})
    assert old.status_code==401,old.text
    new=client.post("/api/login/pin",json={"pin":"813642"})
    assert new.status_code==200,new.text
    assert new.json()["token"]!=waiter_token

    # The owner first activates their PIN using their existing authenticated account.
    activate=client.patch(f"/api/staff/{owner['id']}/pin",headers=auth(owner_token),
       json={"pin":"419253"})
    assert activate.status_code==200 and activate.json()["owner_pin_changed"],activate.text
    owner_login=client.post("/api/login/pin",json={"pin":"419253"})
    assert owner_login.status_code==200 and owner_login.json()["user"]["role"]=="owner",owner_login.text
    # Credentials from before migration stay available to the owner for recovery.
    recovery=client.post("/api/login",json={"username":"propietario_test",
                                             "secret":"LegacyOwner2026!_test"})
    assert recovery.status_code==200,recovery.text
    # PIN-protected staff cannot bypass keypad via old credential login.
    legacy_staff=client.post("/api/login",json={"username":"mesero-uno",
                                                "secret":"anything"})
    assert legacy_staff.status_code in (401,403),legacy_staff.text
print("OK: PIN login, owner recovery, uniqueness, roles and PIN changes")
dbfile.unlink(missing_ok=True)
