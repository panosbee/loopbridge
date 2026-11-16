"""
RAG Chat — DeepSeek V4 Pro + local embeddings.
Απαντάει σε ερωτήσεις χρηστών με βάση τα AAPS/Nightscout docs.
Medical guardrails: ΠΟΤΕ δεν δίνει ιατρικές συμβουλές.
"""
import logging
from typing import List, Optional

from openai import OpenAI
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

from config import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "aaps_docs"

# ─── Load embedding model once ───
_embedding_model: Optional[SentenceTransformer] = None


def get_embedder() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer("BAAI/bge-m3", trust_remote_code=True)
    return _embedding_model


MEDICAL_GUARDRAIL_PROMPT = """Είσαι ο LoopBridge Assistant, ένας τεχνικός βοηθός για χρήστες του AAPS (Android Artificial Pancreas System) και του Nightscout. Μιλάς μόνο Ελληνικά.

## ΠΟΙΟΣ ΕΙΣΑΙ
Είσαι ειδικός σε τεχνικά θέματα: εγκατάσταση, ρυθμίσεις λογισμικού, σύνδεση συσκευών, troubleshooting, και γενικές πληροφορίες για το DIY closed loop. Χρησιμοποιείς απλή, κατανοητή γλώσσα.

## ΚΟΚΚΙΝΗ ΓΡΑΜΜΗ — ΠΟΤΕ
ΔΕΝ είσαι ιατρικός σύμβουλος. ΔΕΝ δίνεις ιατρικές συμβουλές ΠΟΤΕ. Αυτό περιλαμβάνει:
- Δοσολογία ινσουλίνης (basal rates, ISF, ICR, DIA, carb ratio)
- Ερμηνεία τιμών γλυκόζης ("είναι φυσιολογικό το 180;", "τι σημαίνει αυτή η τιμή;")
- Προτάσεις για αλλαγή θεραπείας, φαρμάκων ή σκευασμάτων
- Διάγνωση ή πρόβλεψη ιατρικών καταστάσεων
- Συστάσεις για το αν κάποιος πρέπει ή δεν πρέπει να ξεκινήσει closed loop
- Οποιαδήποτε ερώτηση ξεκινάει με "πόσο να βάλω", "τι δόση", "πρέπει να αλλάξω"

Αν ο χρήστης ρωτήσει κάτι ιατρικό, απάντησε ΑΚΡΙΒΩΣ:
"Αυτό είναι ιατρικό ερώτημα και δεν μπορώ να απαντήσω. Απευθύνσου στον γιατρό σου. Μπορώ να σε βοηθήσω με τεχνικά θέματα όπως εγκατάσταση, σύνδεση, ρυθμίσεις λογισμικού."

## ΤΙ ΜΠΟΡΩ ΝΑ ΚΑΝΩ
- Να εξηγώ πώς λειτουργεί το AAPS, τα Objectives, το Config Builder
- Να βοηθάω με την εγκατάσταση και το browser build
- Να λύνω τεχνικά προβλήματα (σφάλματα σύνδεσης, build errors, GitHub Actions)
- Να εξηγώ πώς συνδέεται το Nightscout με xDrip+, AAPS, Dexcom
- Να δίνω οδηγίες για το Omnipod DASH setup
- Να απαντάω σε ερωτήσεις για ρυθμίσεις λογισμικού

## ΤΟΝΟΣ
- Πάντα στα ελληνικά
- Φιλικός, σαν να μιλάς σε φίλο που έχει διαβήτη
- Αν δεν ξέρεις κάτι, πες το ειλικρινά
- Αν η τεκμηρίωση δεν καλύπτει κάτι, πρότεινε στον χρήστη να ψάξει στα επίσημα docs ή στην κοινότητα

## ΠΛΑΙΣΙΟ (CONTEXT)
Παρακάτω σου δίνονται αποσπάσματα από την επίσημη τεκμηρίωση του AAPS και του Nightscout.
Χρησιμοποίησέ τα για να απαντήσεις με ακρίβεια. Ανάφερε ποιο είναι το θέμα αλλά όχι URLs.
"""


def get_llm_client() -> OpenAI:
    return OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )


def get_qdrant() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url)


def search_docs(query: str, limit: int = 5) -> List[dict]:
    """Ψάχνει στο Qdrant για σχετικά αποσπάσματα (local embeddings)."""
    embedder = get_embedder()
    qdrant = get_qdrant()

    # BGE-m3: query prefix for retrieval
    query_emb = embedder.encode(query, normalize_embeddings=True).tolist()

    results = qdrant.search(
        collection_name=COLLECTION_NAME,
        query_vector=query_emb,
        limit=limit,
    )

    return [
        {
            "text": r.payload["text"],
            "url": r.payload["url"],
            "title": r.payload.get("title", ""),
            "score": r.score,
        }
        for r in results
    ]


def is_medical_question(query: str) -> bool:
    """Ελέγχει αν η ερώτηση είναι ιατρική (guardrail με DeepSeek)."""
    client = get_llm_client()

    check_prompt = f"""Είναι η παρακάτω ερώτηση ΙΑΤΡΙΚΗ (αφορά δοσολογία ινσουλίνης, ερμηνεία τιμών γλυκόζης, 
θεραπευτικές αποφάσεις, διάγνωση, ή αλλαγή φαρμάκων);
Απάντησε ΜΟΝΟ με "ΝΑΙ" ή "ΟΧΙ".

Ερώτηση: {query}
Απάντηση:"""

    try:
        resp = client.chat.completions.create(
            model=settings.deepseek_chat_model,
            messages=[{"role": "user", "content": check_prompt}],
            max_tokens=5,
            temperature=0,
        )
        answer = resp.choices[0].message.content.strip().upper()
        return answer == "ΝΑΙ"
    except Exception as e:
        logger.warning(f"Guardrail check failed: {e}")
        return False


async def chat(query: str) -> dict:
    """Κύρια συνάρτηση chat με RAG — DeepSeek + local embeddings."""
    if not settings.deepseek_api_key:
        return {
            "answer": "Ο βοηθός AI δεν είναι ακόμα διαθέσιμος — χρειάζεται ρύθμιση του DEEPSEEK_API_KEY.",
            "sources": [],
            "blocked": False,
        }

    # ─── GUARDRAIL ───
    try:
        if is_medical_question(query):
            return {
                "answer": "Αυτό είναι ιατρικό ερώτημα και δεν μπορώ να απαντήσω. Απευθύνσου στον γιατρό σου.\n\nΜπορώ να σε βοηθήσω με τεχνικά θέματα: εγκατάσταση AAPS, σύνδεση Nightscout, troubleshooting, build errors, ρυθμίσεις λογισμικού.",
                "sources": [],
                "blocked": True,
            }
    except Exception:
        pass

    # ─── RAG SEARCH ───
    docs = []
    try:
        docs = search_docs(query, limit=5)
    except Exception:
        pass

    # ─── BUILD CONTEXT ───
    context_parts = []
    for d in docs:
        context_parts.append(d["text"])
    context = "\n\n---\n\n".join(context_parts) if context_parts else "(δεν βρέθηκε σχετική τεκμηρίωση)"

    # ─── CHAT ───
    client = get_llm_client()
    messages = [
        {"role": "system", "content": MEDICAL_GUARDRAIL_PROMPT},
        {"role": "system", "content": f"Σχετική τεκμηρίωση:\n\n{context}"},
        {"role": "user", "content": query},
    ]

    resp = client.chat.completions.create(
        model=settings.deepseek_chat_model,
        messages=messages,
        max_tokens=4096,
        temperature=0.3,
    )

    answer = resp.choices[0].message.content

    return {
        "answer": answer,
        "sources": [{"url": d["url"], "title": d["title"]} for d in docs],
        "blocked": False,
    }
