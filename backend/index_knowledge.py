"""Index an externally supplied educational-content ZIP into tenant knowledge collections.

Run from backend/: python index_knowledge.py --site responseready --archive <path>
Alternatively set KNOWLEDGE_ARCHIVE_PATH. The ZIP contains UTF-8 pages under
output/<site-directory>/pages/<page-slug>.md, with a title and source URL.
Policy answers remain in curated prompt content and are excluded from indexing.
Indexing replaces the selected knowledge collection; use the intended environment.
"""

import argparse
import hashlib
import os
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SITE_DIRS = {
    "responseready": "responseready_com",
    "cpr1": "cpr1_com",
    "mdsimedical": "mdsimedical_com",
    "aedtotalsolution": "aedtotalsolution_com",
    # AED Leader uses curated prompts until a corresponding corpus is onboarded.
}

# Policy content is owned by curated prompts rather than retrieval.
EXCLUDED_SLUG_PARTS = (
    "refund-policy", "shipping-policy", "terms", "privacy", "contact",
    "careers", "meet-our-team", "submit-an-inquiry", "submit-your-inquiry",
    "blog", "client-testimonials", "get-a-quote",
)

MIN_PAGE_CHARS = 400        # thin/nav pages carry no answerable knowledge
TARGET_CHUNK_CHARS = 2000   # ≈500 tokens
MIN_SECTION_CHARS = 200     # tiny sections merge into their neighbor


def knowledge_collection(slug: str) -> str:
    return f"aed_knowledge_{slug}"


def _clean_body(body: str) -> str:
    lines = []
    for line in body.split("\n"):
        ls = line.strip()
        if ls.startswith("![") or ls.startswith("* Published") or ls.startswith("* [ Written By"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_page(title: str, body: str) -> list[tuple[str, str]]:
    """(section_heading, section_text) pairs — heading-based, merged, size-capped."""
    raw_sections = re.split(r"\n(?=##? )", body)
    merged: list[tuple[str, str]] = []
    for sec in raw_sections:
        sec = sec.strip()
        if not sec:
            continue
        first, _, rest = sec.partition("\n")
        if first.startswith("#"):
            heading, content = first.lstrip("# ").strip(), rest.strip()
        else:
            heading, content = "", sec
        if merged and (len(content) < MIN_SECTION_CHARS or not content):
            ph, pc = merged[-1]
            merged[-1] = (ph, (pc + "\n\n" + (heading + "\n" if heading else "") + content).strip())
        else:
            merged.append((heading or title, content))

    def _accumulate(pieces, sep, cap):
        buf, out = "", []
        for piece in pieces:
            if buf and len(buf) + len(piece) > cap:
                out.append(buf.strip())
                buf = piece
            else:
                buf = (buf + sep + piece).strip()
        if buf.strip():
            out.append(buf.strip())
        return out

    chunks: list[tuple[str, str]] = []
    for heading, content in merged:
        if len(content) <= TARGET_CHUNK_CHARS:
            chunks.append((heading, content))
            continue
        # paragraph boundaries first; sections with no blank lines (tables,
        # bullet runs) fall back to single-newline splits, then a hard char
        # window — a chunk must NEVER exceed the embedding token limit
        for part in _accumulate(content.split("\n\n"), "\n\n", TARGET_CHUNK_CHARS):
            if len(part) <= TARGET_CHUNK_CHARS * 2:
                chunks.append((heading, part))
                continue
            for line_part in _accumulate(part.split("\n"), "\n", TARGET_CHUNK_CHARS):
                if len(line_part) <= TARGET_CHUNK_CHARS * 2:
                    chunks.append((heading, line_part))
                else:
                    for i in range(0, len(line_part), TARGET_CHUNK_CHARS):
                        chunks.append((heading, line_part[i:i + TARGET_CHUNK_CHARS]))
    return [(h, c) for h, c in chunks if len(c) >= MIN_SECTION_CHARS]


def load_chunks(slug: str, archive_path: str | os.PathLike | None = None) -> list[dict]:
    archive_path = archive_path or os.getenv("KNOWLEDGE_ARCHIVE_PATH")
    if not archive_path:
        raise ValueError("Supply an archive path or set KNOWLEDGE_ARCHIVE_PATH.")
    with zipfile.ZipFile(archive_path) as zf:
        return _load_archive_chunks(zf, slug)


def _load_archive_chunks(zf: zipfile.ZipFile, slug: str) -> list[dict]:
    prefix = f"output/{SITE_DIRS[slug]}/pages/"
    out = []
    skipped_policy = skipped_thin = 0
    for name in zf.namelist():
        if not (name.startswith(prefix) and name.endswith(".md")):
            continue
        page_slug = os.path.basename(name)[:-3]
        if any(part in page_slug for part in EXCLUDED_SLUG_PARTS):
            skipped_policy += 1
            continue
        text = zf.read(name).decode("utf-8", "replace")
        lines = text.split("\n")
        title = lines[0].lstrip("# ").strip() if lines else page_slug
        url = ""
        for line in lines[1:4]:
            if line.startswith("URL:"):
                url = line[4:].strip()
                break
        body = _clean_body("\n".join(lines[2:]))
        if len(body) < MIN_PAGE_CHARS:
            skipped_thin += 1
            continue
        for i, (section, content) in enumerate(_split_page(title, body)):
            chunk_text = f"{title} — {section}\n\n{content}" if section != title else f"{title}\n\n{content}"
            out.append({
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{slug}/{page_slug}/{i}")),
                "text": chunk_text,
                "payload": {
                    "tenant": slug,
                    "page_title": title,
                    "page_slug": page_slug,
                    "section": section,
                    "chunk_index": i,
                    "source_url": url,
                    "text": chunk_text,
                    "content_hash": hashlib.sha256(chunk_text.encode()).hexdigest(),
                    "last_synced_at": datetime.now(timezone.utc).isoformat(),
                },
            })
    print(f"[{slug}] chunks: {len(out)} | pages skipped: {skipped_policy} policy, {skipped_thin} thin")
    return out


def index_site(slug: str, archive_path: str | os.PathLike | None = None) -> None:
    coll = knowledge_collection(slug)
    chunks = load_chunks(slug, archive_path)
    if not chunks:
        raise ValueError(f"No eligible knowledge chunks for {slug}; collection left unchanged.")

    # Initialize database and embedding clients only after validating local input.
    from index_to_qdrant import (
        qdrant_client, embed_dense, embed_sparse, EMBEDDING_DIM, BATCH_SIZE,
    )
    from qdrant_client.models import Distance, VectorParams, SparseVectorParams, PointStruct

    existing = [c.name for c in qdrant_client.get_collections().collections]
    if coll in existing:
        qdrant_client.delete_collection(coll)
    qdrant_client.create_collection(
        collection_name=coll,
        vectors_config={"dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )
    for start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[start:start + BATCH_SIZE]
        texts = [c["text"] for c in batch]
        dense = embed_dense(texts)
        sparse = embed_sparse(texts)
        qdrant_client.upsert(collection_name=coll, points=[
            PointStruct(id=c["id"], vector={"dense": d, "sparse": s}, payload=c["payload"])
            for c, d, s in zip(batch, dense, sparse)
        ])
        print(f"  upserted {start + len(batch)}/{len(chunks)}")
    print(f"[{slug}] done — {qdrant_client.get_collection(coll).points_count} points in {coll}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    sites = parser.add_mutually_exclusive_group(required=True)
    sites.add_argument("--site", choices=sorted(SITE_DIRS))
    sites.add_argument("--all-sisters", action="store_true")
    parser.add_argument("--archive", default=os.getenv("KNOWLEDGE_ARCHIVE_PATH"),
                        help="External knowledge ZIP; defaults to KNOWLEDGE_ARCHIVE_PATH")
    args = parser.parse_args()
    if not args.archive:
        parser.error("Supply --archive <path> or set KNOWLEDGE_ARCHIVE_PATH.")
    archive = Path(args.archive).expanduser()
    if not archive.is_file():
        parser.error(f"Knowledge archive not found: {archive}")
    slugs = list(SITE_DIRS) if args.all_sisters else [args.site]
    try:
        # Preflight every selected site before replacing any collection.
        for slug in slugs:
            if not load_chunks(slug, archive):
                raise ValueError(f"No eligible knowledge chunks for {slug}; collections left unchanged.")
        for slug in slugs:
            index_site(slug, archive)
    except (OSError, zipfile.BadZipFile, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
