import re
import logging
from typing import Optional

import docker
from docker.models.containers import Container
from docker.errors import NotFound, APIError

from config import settings

logger = logging.getLogger(__name__)
docker_client = docker.from_env()

SUBDOMAIN_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}$")
SUBDOMAIN_BLACKLIST = {
    "app", "api", "www", "mail", "admin", "ns", "nightscout",
    "loopbridge", "dashboard", "status", "help", "support",
    "localhost", "test", "demo", "staging", "dev",
}


def validate_subdomain(subdomain: str) -> bool:
    if not SUBDOMAIN_REGEX.match(subdomain):
        return False
    if subdomain.lower() in SUBDOMAIN_BLACKLIST:
        return False
    return True


def create_mongo_database(mongo_user: str, mongo_pass: str, db_name: str) -> bool:
    """Δημιουργεί χρήστη και database στη MongoDB (Docker)."""
    try:
        from pymongo import MongoClient
        from pymongo.errors import OperationFailure

        # Docker MongoDB — συνδέεται μέσω localhost (exposed port)
        if settings.mongo_admin_user:
            uri = f"mongodb://{settings.mongo_admin_user}:{settings.mongo_admin_pass}@127.0.0.1:27018/admin"
        else:
            uri = "mongodb://127.0.0.1:27018/admin"
        client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")

        db = client[db_name]
        try:
            db.command("createUser", mongo_user, pwd=mongo_pass,
                       roles=[{"role": "readWrite", "db": db_name}])
            logger.info(f"MongoDB user {mongo_user} created on {db_name}")
        except OperationFailure as e:
            if e.code == 51003:
                db.command("updateUser", mongo_user, pwd=mongo_pass)
                logger.info(f"MongoDB user {mongo_user} already existed, password updated")
            else:
                raise
        client.close()
        return True
    except Exception as e:
        logger.exception(f"MongoDB error: {e}")
        return False


def create_nightscout_container(
    subdomain: str,
    mongo_user: str,
    mongo_pass: str,
    mongo_db: str,
    api_secret: str,
) -> Optional[str]:
    """Δημιουργεί και ξεκινάει ένα Nightscout container."""
    container_name = f"ns-{subdomain}"

    # Έλεγχος αν υπάρχει ήδη
    try:
        old = docker_client.containers.get(container_name)
        old.remove(force=True)
        logger.info(f"Removed existing container: {container_name}")
    except NotFound:
        pass

    is_local = settings.domain == "localhost"

    # Nightscout container βλέπει το MongoDB στο ns-mongo (ίδιο Docker network)
    mongo_uri = (
        f"mongodb://{mongo_user}:{mongo_pass}"
        f"@ns-mongo:27017/{mongo_db}"
        f"?authSource={mongo_db}"
    )

    labels = {}
    network_name = "ns-net"
    if is_local:
        ports = {"1337/tcp": None}
        network_name = settings.docker_network
    else:
        ports = {"1337/tcp": ("127.0.0.1", None)}

    env_vars = {
        "NODE_ENV": "production",
        "TZ": "Europe/Athens",
        "INSECURE_USE_HTTP": "true",
        "MONGO_CONNECTION": mongo_uri,
        "API_SECRET": api_secret,
        "DISPLAY_UNITS": "mg/dl",
        "ENABLE": "careportal basal iob cob bwp cage sage iage bage pump openaps loop override dbsize",
        "SHOW_FORECAST": "openaps",
        "AUTH_DEFAULT_ROLES": "denied",
        "THEME": "colors",
        "PORT": "1337",
    }

    try:
        container_kwargs = {
            "image": settings.nightscout_image,
            "name": container_name,
            "detach": True,
            "restart_policy": {"Name": "unless-stopped"},
            "mem_limit": "384m",
            "environment": env_vars,
            "labels": labels,
            "network": network_name,
        }
        if ports:
            container_kwargs["ports"] = ports
        container: Container = docker_client.containers.run(**container_kwargs)
        if ports:
            container.reload()
            assigned_port = container.ports.get("1337/tcp", [{}])[0].get("HostPort", "unknown")
            logger.info(f"Local Nightscout accessible at http://localhost:{assigned_port}")
        logger.info(f"Container {container_name} started: {container.id[:12]}")
        return container.id
    except APIError as e:
        logger.error(f"Docker API error: {e}")
        return None


def healthcheck_nightscout(subdomain: str) -> bool:
    """Ελέγχει αν το Nightscout είναι live."""
    import httpx
    try:
        if settings.domain == "localhost":
            url = f"http://ns-{subdomain}:1337/api/v1/status.json"
        else:
            c = docker_client.containers.get(f"ns-{subdomain}")
            c.reload()
            host_port = c.ports.get("1337/tcp", [{}])[0].get("HostPort")
            if not host_port:
                return False
            url = f"http://127.0.0.1:{host_port}/api/v1/status.json"
        r = httpx.get(url, timeout=10, verify=False)
        return r.status_code in (200, 401)
    except Exception:
        return False


def stop_nightscout(subdomain: str) -> bool:
    """Σταματάει το container (suspend)."""
    try:
        c = docker_client.containers.get(f"ns-{subdomain}")
        c.stop()
        return True
    except NotFound:
        return True


def remove_nightscout(subdomain: str) -> bool:
    """Διαγράφει το container."""
    try:
        c = docker_client.containers.get(f"ns-{subdomain}")
        c.remove(force=True)
        return True
    except NotFound:
        return True
