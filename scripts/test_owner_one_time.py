"""Validación local de recuperación idempotente del acceso del propietario."""
import os, secrets, tempfile
from pathlib import Path
from uuid import uuid4
from datetime import timedelta

path=Path(tempfile.gettempdir())/("mitote-owner-"+uuid4().hex+".sqlite")
os.environ["DATABASE_URL"]="sqlite:///"+str(path)
os.environ["ADMIN_USERNAME"]="owner-ci"
os.environ["ADMIN_PASSWORD"]=secrets.token_urlsafe(24)
os.environ["SECRET_KEY"]=secrets.token_urlsafe(36)
os.environ["OWNER_RECOVERY_TOKEN"]=secrets.token_urlsafe(36)
os.environ["OWNER_RECOVERY_PASSWORD"]=secrets.token_urlsafe(26)
os.environ["OWNER_RECOVERY_PIN"]=str(secrets.randbelow(900000)+100000)
import app
from fastapi.testclient import TestClient

with TestClient(app.app) as client:
    pin=os.environ["OWNER_RECOVERY_PIN"]
    user=client.post("/api/login/pin",json={"pin":pin})
    assert user.status_code==200,user.text
    assert user.json()["user"]["role"]=="owner"
    with app.SessionLocal() as db:
        owner=db.scalar(app.select(app.User).where(app.User.role=="owner"))
        version=owner.session_version
        assert app.ph.verify(owner.password_hash,os.environ["OWNER_RECOVERY_PASSWORD"])
        assert db.scalar(app.select(app.func.count()).select_from(app.OwnerRecoveryEvent))==1
    app.bootstrap()
    with app.SessionLocal() as db:
        assert db.get(app.User,owner.id).session_version==version
        assert db.scalar(app.select(app.func.count()).select_from(app.OwnerRecoveryEvent))==1

print("OK: recuperación de propietario sin credenciales impresas y sin repetición al reiniciar")
path.unlink(missing_ok=True)
