"""
RAG Ingestion Pipeline — DeepSeek + local embeddings
Σαρώνει τα AAPS/Nightscout docs και τα αποθηκεύει στο Qdrant.
Embeddings: BGE-m3 (multilingual, Ελληνικά + Αγγλικά)
"""
import hashlib
import logging
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from qdrant_client import QdrantClient
from qdrant_client.http import models
from sentence_transformers import SentenceTransformer

from config import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "aaps_docs"
VECTOR_SIZE = 1024  # BGE-m3 output dims
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# ─── Embedding model (loaded once) ───
_embedding_model: Optional[SentenceTransformer] = None


def get_embedder() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer("BAAI/bge-m3", trust_remote_code=True)
        logger.info("Loaded BGE-m3 embedding model")
    return _embedding_model


# ─── ΠΗΓΕΣ ────────────────────────────────────────────
SOURCES = [
    {
        "name": "AAPS English",
        "url": "https://androidaps.readthedocs.io/en/latest/",
        "allowed_prefix": "https://androidaps.readthedocs.io/en/latest/",
        "max_pages": 120,
        "language": "en",
    },
    {
        "name": "AAPS Greek",
        "url": "https://androidaps-el.readthedocs.io/el/latest/",
        "allowed_prefix": "https://androidaps-el.readthedocs.io/el/latest/",
        "max_pages": 80,
        "language": "el",
    },
    {
        "name": "Nightscout",
        "url": "https://nightscout.github.io/",
        "allowed_prefix": "https://nightscout.github.io/",
        "max_pages": 60,
        "language": "en",
    },
]


def get_qdrant() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url)


def ensure_collection(client: QdrantClient):
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=VECTOR_SIZE,
                distance=models.Distance.COSINE,
            ),
        )
        logger.info(f"Created Qdrant collection: {COLLECTION_NAME}")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Σπάει το κείμενο σε chunks με overlap."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            for sep in [". ", "\n\n", "\n", " "]:
                last_sep = text.rfind(sep, start, end)
                if last_sep > start + chunk_size // 2:
                    end = last_sep + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap if end < len(text) else end
    return chunks


async def fetch_page(url: str) -> Optional[str]:
    """Φέρνει το περιεχόμενο μιας σελίδας, εξάγει το κείμενο."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "lxml")

            for tag in soup(["nav", "footer", "script", "style", "header", ".sidebar", ".wy-nav-side"]):
                tag.decompose()

            content = soup.find("div", class_="document") or \
                      soup.find("main") or \
                      soup.find("article") or \
                      soup.find("body")

            if not content:
                return None

            text = content.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"Fetch failed {url}: {e}")
            return None


async def crawl_source(source: dict, client: QdrantClient, embedder: SentenceTransformer) -> int:
    """Σαρώνει ένα site και αποθηκεύει τα chunks στο Qdrant."""
    base = source["url"]
    allowed = source["allowed_prefix"]
    max_pages = source["max_pages"]
    name = source["name"]

    visited: set[str] = set()
    to_visit: List[str] = [base]
    stored = 0

    async with httpx.AsyncClient(timeout=30) as http:
        while to_visit and len(visited) < max_pages:
            url = to_visit.pop(0)
            url = url.split("#")[0].rstrip("/")
            if url in visited:
                continue
            if not url.startswith(allowed.rstrip("/")):
                continue
            visited.add(url)

            logger.info(f"[{name}] Fetching ({len(visited)}/{max_pages}): {url}")
            text = await fetch_page(url)
            if not text:
                continue

            # Crawl links
            try:
                r = await http.get(url, follow_redirects=True)
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = urljoin(url, a["href"]).split("#")[0].rstrip("/")
                    if href.startswith(allowed) and href not in visited:
                        to_visit.append(href)
            except Exception:
                pass

            # Chunk
            chunks = chunk_text(text)
            if not chunks:
                continue

            # BGE-m3: add instruction prefix for retrieval
            doc_id = hashlib.md5(url.encode()).hexdigest()[:12]
            points = []
            for i, chunk in enumerate(chunks):
                emb = embedder.encode(chunk, normalize_embeddings=True).tolist()
                points.append(models.PointStruct(
                    id=hashlib.md5(f"{url}#chunk{i}".encode()).hexdigest(),
                    vector=emb,
                    payload={
                        "url": url,
                        "title": name,
                        "text": chunk,
                        "language": source["language"],
                        "doc_id": doc_id,
                        "chunk_index": i,
                    },
                ))

            client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
            stored += len(points)
            logger.info(f"[{name}] Stored {len(points)} chunks from {url}")

    logger.info(f"[{name}] Done: {stored} chunks from {len(visited)} pages")
    return stored


async def ingest_all() -> dict:
    """Σαρώνει όλες τις πηγές και επιστρέφει summary."""
    embedder = get_embedder()
    qdrant = get_qdrant()
    ensure_collection(qdrant)

    results = {}
    for source in SOURCES:
        logger.info(f"Starting crawl: {source['name']}")
        try:
            count = await crawl_source(source, qdrant, embedder)
            results[source["name"]] = count
        except Exception as e:
            results[source["name"]] = f"error: {e}"

    return results
