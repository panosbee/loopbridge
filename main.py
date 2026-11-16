"""
LoopBridge MVP — FastAPI Control Plane
Endpoints: auth, Nightscout provisioning, GitHub OAuth, AAPS wizard
"""
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from pydantic import BaseModel, EmailStr

from config import settings
from models import Base, User, Tenant, GitHubLink, BuildJob
from auth import (
    hash_password, verify_password, create_access_token, decode_token,
    generate_api_secret, generate_mongo_password, oauth2_scheme,
)
from provisioner import (
    validate_subdomain, create_mongo_database, create_nightscout_container,
    healthcheck_nightscout, stop_nightscout,
)
from github_client import (
    get_oauth_url, exchange_code_for_token, get_user_login,
    fork_repo, enable_actions, setup_all_secrets,
    find_workflow_id, dispatch_build, get_run_status,
    get_latest_aaps_version, sync_fork,
)
from rag_chat import chat as rag_chat
from rag_ingest import ingest_all

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title="LoopBridge API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_db():
    async with async_session() as session:
        yield session


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    import uuid
    user_id = uuid.UUID(decode_token(token))
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Ο χρήστης δεν βρέθηκε")
    return user


# ──────────────────── Pydantic Schemas ────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str = ""
    accept_tos: bool = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TenantCreateRequest(BaseModel):
    subdomain: str


class WizardSecretsRequest(BaseModel):
    keystore_set: str   # base64(KEYSTORE_BASE64|PASS|ALIAS|KEY_PASS)
    gdrive_oauth2: str  # base64(CLIENT_ID|REFRESH_TOKEN)


class BuildRequest(BaseModel):
    version: str = ""
    build_variant: str = "fullRelease"


# ──────────────────── AUTH ────────────────────

@app.post("/auth/register")
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    existing = (await db.execute(select(User).where(User.email == req.email))).scalar()
    if existing:
        raise HTTPException(status_code=400, detail="Το email χρησιμοποιείται ήδη")

    user = User(
        email=req.email,
        pass_hash=hash_password(req.password),
        name=req.name,
        tos_accepted_at=None,  # θα μπει timestamp αν accept_tos
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(str(user.id))
    return {"access_token": token, "token_type": "bearer", "user_id": str(user.id)}


@app.post("/auth/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    user = (await db.execute(select(User).where(User.email == req.email))).scalar()
    if not user or not verify_password(req.password, user.pass_hash):
        raise HTTPException(status_code=401, detail="Λάθος email ή κωδικός")

    token = create_access_token(str(user.id))
    return {"access_token": token, "token_type": "bearer", "user_id": str(user.id)}


@app.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "created_at": user.created_at.isoformat(),
    }


# ──────────────────── TENANTS (Nightscout) ────────────────────

@app.post("/tenants")
async def create_tenant(
    req: TenantCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select

    # Έλεγξε αν υπάρχει ήδη tenant
    existing = (await db.execute(select(Tenant).where(Tenant.user_id == user.id))).scalar()
    if existing:
        raise HTTPException(status_code=400, detail="Έχεις ήδη Nightscout instance")

    subdomain = req.subdomain.lower().strip()
    if not validate_subdomain(subdomain):
        raise HTTPException(
            status_code=400,
            detail="Μη έγκυρο subdomain (3-30 χαρακτήρες, μόνο πεζά λατινικά και παύλες)",
        )

    # Έλεγξε μοναδικότητα
    dup = (await db.execute(select(Tenant).where(Tenant.subdomain == subdomain))).scalar()
    if dup:
        raise HTTPException(status_code=400, detail="Το subdomain χρησιμοποιείται ήδη")

    api_secret = generate_api_secret()
    mongo_user = f"u_{subdomain}"
    mongo_pass = generate_mongo_password()
    mongo_db = f"ns_{subdomain}"

    # Provisioning state machine
    tenant = Tenant(
        user_id=user.id,
        subdomain=subdomain,
        status="validating",
    )
    db.add(tenant)
    await db.commit()

    # Step 1: MongoDB
    tenant.status = "db_creating"
    await db.commit()
    if not create_mongo_database(mongo_user, mongo_pass, mongo_db):
        tenant.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Αποτυχία δημιουργίας βάσης δεδομένων")

    # Step 2: Container
    tenant.status = "container_starting"
    await db.commit()
    container_id = create_nightscout_container(subdomain, mongo_user, mongo_pass, mongo_db, api_secret)
    if not container_id:
        tenant.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Αποτυχία εκκίνησης container")

    # Step 3: Healthcheck
    tenant.status = "healthcheck"
    await db.commit()

    import asyncio
    for i in range(12):  # 12 * 5s = 60s
        await asyncio.sleep(5)
        if healthcheck_nightscout(subdomain):
            break
    else:
        tenant.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Το Nightscout δεν ανταποκρίνεται")

    # Success
    tenant.status = "active"
    tenant.api_secret_enc = api_secret   # TODO: encrypt with Fernet
    tenant.mongo_user = mongo_user
    tenant.mongo_db = mongo_db
    await db.commit()

    return {
        "id": str(tenant.id),
        "subdomain": subdomain,
        "url": f"https://{subdomain}.{settings.domain}",
        "api_secret": api_secret,
        "status": tenant.status,
        "_note": "Φύλαξε το API secret — εμφανίζεται μόνο μία φορά",
    }


@app.get("/tenants/me")
async def get_my_tenant(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    tenant = (await db.execute(select(Tenant).where(Tenant.user_id == user.id))).scalar()
    if not tenant:
        raise HTTPException(status_code=404, detail="Δεν έχεις Nightscout instance")

    is_healthy = healthcheck_nightscout(tenant.subdomain)

    return {
        "id": str(tenant.id),
        "subdomain": tenant.subdomain,
        "url": f"https://{tenant.subdomain}.{settings.domain}",
        "status": tenant.status,
        "healthy": is_healthy,
        "created_at": tenant.created_at.isoformat(),
    }


# ──────────────────── GITHUB OAUTH ────────────────────

@app.get("/auth/github/login")
async def github_login(user: User = Depends(get_current_user)):
    """Επιστρέφει το URL για GitHub OAuth."""
    url, state = get_oauth_url(str(user.id))
    return {"url": url, "state": state}


@app.get("/auth/github/callback")
async def github_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Callback για GitHub OAuth — no auth required (το user_id είναι στο state)."""
    import uuid

    # Extract user_id from state (format: csrf_token:user_id — user_id is LAST UUID)
    parts = state.split(":")
    user_id = None
    # Πάρε το τελευταίο UUID (user_id είναι πάντα τελευταίο)
    for part in reversed(parts):
        try:
            user_id = str(uuid.UUID(part))
            break
        except ValueError:
            continue

    if not user_id:
        raise HTTPException(status_code=400, detail="Δεν βρέθηκε user_id στο state")

    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="Ο χρήστης δεν βρέθηκε")

    # Ανταλλαγή code για token
    token_data = await exchange_code_for_token(code)
    if not token_data:
        raise HTTPException(status_code=400, detail="Αποτυχία σύνδεσης με GitHub")

    access_token = token_data["access_token"]
    gh_login = await get_user_login(access_token)
    if not gh_login:
        raise HTTPException(status_code=400, detail="Αδυναμία λήψης GitHub username")

    # Κρυπτογράφηση και αποθήκευση
    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode() if settings.encryption_key else Fernet.generate_key())
    encrypted_token = fernet.encrypt(access_token.encode()).decode()

    # Αποθήκευση (update αν υπάρχει ήδη)
    from sqlalchemy import select
    existing = (await db.execute(select(GitHubLink).where(GitHubLink.user_id == user.id))).scalar()
    if existing:
        existing.gh_login = gh_login
        existing.access_token_enc = encrypted_token
        existing.scopes = "repo,workflow"
        existing.linked_at = None  # θα πάρει default τρέχουσα ώρα
        existing.revoked_at = None
    else:
        link = GitHubLink(
            user_id=user.id,
            gh_login=gh_login,
            access_token_enc=encrypted_token,
            scopes="repo,workflow",
        )
        db.add(link)
    await db.commit()

    # Επιστροφή HTML που κλείνει το popup
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>LoopBridge - GitHub Connected</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f0fdf4;}}
.card{{background:white;padding:2rem;border-radius:16px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,0.1);}}
h2{{color:#16a34a;}} p{{color:#4b5563;}} button{{margin-top:1rem;padding:0.7rem 1.5rem;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer;font-size:1rem;}}
</style></head>
<body><div class="card"><h2>✅ Συνδέθηκες ως {gh_login}!</h2>
<p>Το GitHub συνδέθηκε με το LoopBridge. Μπορείς να κλείσεις αυτό το παράθυρο.</p>
<button onclick="window.close()">Κλείσιμο</button></div>
<script>if(window.opener){{window.opener.postMessage('github_connected','*');setTimeout(function(){{window.close()}},1000);}}</script>
</body></html>""")


# ──────────────────── AAPS WIZARD ────────────────────

@app.get("/wizard/github-status")
async def github_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ελέγχει αν ο χρήστης έχει συνδέσει GitHub."""
    link = await db.get(GitHubLink, user.id)
    return {"connected": link is not None, "login": link.gh_login if link else None}


@app.post("/wizard/fork")
async def wizard_fork(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Βήμα 1: Fork του AndroidAPS."""
    link = await db.get(GitHubLink, user.id)
    if not link:
        raise HTTPException(status_code=400, detail="Συνδέσου πρώτα με GitHub (/auth/github/login)")

    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode())
    access_token = fernet.decrypt(link.access_token_enc.encode()).decode()

    result = await fork_repo(access_token, link.gh_login)
    if not result:
        raise HTTPException(status_code=500, detail="Αποτυχία fork — δοκίμασε ξανά")

    await enable_actions(access_token, link.gh_login)

    return {
        "status": "forked",
        "repo_url": result["html_url"],
        "message": f"Το προσωπικό σου αντίγραφο του AAPS είναι έτοιμο",
    }


@app.post("/wizard/secrets")
async def wizard_secrets(
    req: WizardSecretsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Βήμα 2: Αποθήκευση secrets (keystore + GDrive) στο fork."""
    link = await db.get(GitHubLink, user.id)
    if not link:
        raise HTTPException(status_code=400, detail="Συνδέσου πρώτα με GitHub")

    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode())
    access_token = fernet.decrypt(link.access_token_enc.encode()).decode()

    results = await setup_all_secrets(
        access_token,
        link.gh_login,
        keystore_set=req.keystore_set,
        gdrive_oauth2=req.gdrive_oauth2,
    )

    failed = [k for k, v in results.items() if not v]
    if failed:
        raise HTTPException(status_code=500, detail=f"Αποτυχία στα secrets: {', '.join(failed)}")

    return {
        "status": "secrets_configured",
        "message": "Τα κλειδιά υπογραφής και το Google Drive ρυθμίστηκαν",
    }


@app.post("/wizard/build")
async def wizard_build(
    req: BuildRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Βήμα 3: Χτίσιμο του APK."""
    link = await db.get(GitHubLink, user.id)
    if not link:
        raise HTTPException(status_code=400, detail="Συνδέσου πρώτα με GitHub")

    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode())
    access_token = fernet.decrypt(link.access_token_enc.encode()).decode()

    version = req.version or await get_latest_aaps_version()

    workflow_id = await find_workflow_id(access_token, link.gh_login)
    if not workflow_id:
        raise HTTPException(status_code=500, detail="Δεν βρέθηκε το AAPS-CI workflow")

    run_id = await dispatch_build(access_token, link.gh_login, workflow_id, version, req.build_variant)
    if not run_id:
        raise HTTPException(status_code=500, detail="Αποτυχία έναρξης build")

    # Αποθήκευση job
    job = BuildJob(
        user_id=user.id,
        gh_repo=f"{link.gh_login}/AndroidAPS",
        workflow_run_id=run_id,
        aaps_version=version,
        build_variant=req.build_variant,
        status="queued",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    return {
        "job_id": str(job.id),
        "run_id": run_id,
        "version": version,
        "status": "queued",
        "message": "Το build ξεκίνησε — θα πάρει 20-40 λεπτά",
    }


@app.get("/wizard/build/{job_id}")
async def wizard_build_status(
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ελέγχει την κατάσταση ενός build."""
    job = await db.get(BuildJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Το job δεν βρέθηκε")

    if job.status in ("success", "failure", "cancelled"):
        return {
            "job_id": str(job.id),
            "status": job.status,
            "version": job.aaps_version,
            "error_code": job.error_code,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }

    link = await db.get(GitHubLink, user.id)
    if not link:
        raise HTTPException(status_code=400, detail="GitHub not linked")

    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode())
    access_token = fernet.decrypt(link.access_token_enc.encode()).decode()

    run = await get_run_status(access_token, link.gh_login, job.workflow_run_id)
    if run:
        git_status = run["status"]
        conclusion = run.get("conclusion")

        if git_status == "completed":
            job.status = conclusion or "completed"
            job.finished_at = None  # σε production βάλε datetime.utcnow()
            if conclusion == "failure":
                job.error_code = "build_failed"
            await db.commit()
        elif git_status in ("queued", "in_progress"):
            job.status = git_status
            await db.commit()

        return {
            "job_id": str(job.id),
            "status": job.status,
            "version": job.aaps_version,
            "build_url": run.get("html_url"),
            "error_code": job.error_code,
        }

    return {"job_id": str(job.id), "status": job.status, "version": job.aaps_version}


@app.get("/wizard/aaps-version")
async def get_version():
    """Επιστρέφει την τελευταία έκδοση του AAPS."""
    version = await get_latest_aaps_version()
    return {"version": version}


@app.post("/wizard/sync-fork")
async def wizard_sync_fork(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Συγχρονίζει το fork με upstream."""
    link = await db.get(GitHubLink, user.id)
    if not link:
        raise HTTPException(status_code=400, detail="Συνδέσου πρώτα με GitHub")

    from cryptography.fernet import Fernet
    fernet = Fernet(settings.encryption_key.encode())
    access_token = fernet.decrypt(link.access_token_enc.encode()).decode()

    ok = await sync_fork(access_token, link.gh_login)
    return {"synced": ok, "message": "Το fork συγχρονίστηκε" if ok else "Ο συγχρονισμός απέτυχε"}


# ──────────────────── HEALTH ────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ──────────────────── RAG / AI CHAT ────────────────────

class ChatRequest(BaseModel):
    query: str


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """AI βοηθός με RAG (DeepSeek) — απαντάει σε τεχνικές ερωτήσεις για AAPS/Nightscout."""
    result = await rag_chat(req.query)
    return result


@app.post("/admin/ingest-docs")
async def ingest_docs_endpoint():
    """Σαρώνει τα AAPS/Nightscout docs και τα αποθηκεύει στο Qdrant."""
    result = await ingest_all()
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
