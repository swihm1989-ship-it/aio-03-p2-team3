"""상품 검색은 근거를 반환하고, 가격·재고는 구조화 DB의 값을 보존한다."""
from decimal import Decimal, InvalidOperation

from app.catalog.repository import DIMENSIONS, MODEL
from app.knowledge.schemas import KnowledgeError, ModelProfile

PROFILE = ModelProfile(provider="openai", model=MODEL, dimensions=DIMENSIONS,
                       revision="openai-embeddings-v1")
PREFIX = "Shared catalog: "


def index_catalog(repository, owner, embedder, *, batch_size=32):
    if embedder.profile != PROFILE or not 1 <= batch_size <= 32:
        raise KnowledgeError("catalog_embedding_profile", "공용 상품은 text-embedding-3-small/1536 프로필을 사용합니다.", 503)
    snapshots = repository.pending(owner)
    vectors = []
    for offset in range(0, len(snapshots), batch_size):
        batch_rows = snapshots[offset:offset + batch_size]
        batch = embedder.embed_documents([r["content"] for r in batch_rows])
        batch.validate_for(len(batch_rows), PROFILE)
        if any(not any(vector) for vector in batch.vectors):
            raise KnowledgeError("embedding_mismatch", "0으로만 구성된 임베딩은 저장할 수 없습니다.")
        vectors.extend(batch.vectors)
    if snapshots:
        # Publish once, after every batch succeeds. A failed provider call never
        # leaves a partially completed index or destroys an existing good vector.
        repository.publish(owner, snapshots, vectors)
    return {**repository.status(owner), "newly_indexed": len(snapshots), "model": MODEL,
            "dimensions": DIMENSIONS}


def retrieve(repository, owner, query, *, embedder=None):
    matches = repository.search(owner, query)
    if any(row["retrieval_method"] == "exact_identifier" for row in matches):
        return matches
    if embedder is None or not repository.status(owner)["indexed"]:
        return matches
    if embedder.profile != PROFILE:
        raise KnowledgeError("catalog_embedding_profile", "상품 검색 임베딩 프로필이 맞지 않습니다.", 503)
    batch = embedder.embed_documents([query])
    batch.validate_for(1, PROFILE)
    return repository.search(owner, query, vector=batch.vectors[0])


def enrich_document(draft, lookup):
    """Only exact identities fill a proposed list price; ambiguity never selects a variant."""
    if lookup is None or draft.get("document_type") not in {"PI", "PO"}:
        return
    draft["missing_fields"] = [v for v in draft.get("missing_fields", []) if not str(v).startswith(PREFIX)]
    evidence, exact_items = [], 0
    items = draft.get("items", [])
    for position, item in enumerate(items, 1):
        if item.get("is_sample") is True:
            continue
        query = item.get("product_code") or item.get("product_name")
        if not query:
            continue
        matches = lookup(query)
        if not matches:
            continue
        evidence.append({"item": position, "candidates": matches})
        if len(matches) != 1 or matches[0]["retrieval_method"] != "exact_identifier":
            # Semantic candidates are suggestions, never proof that an unrelated
            # user-entered product must be replaced by a catalog product.
            if any(r["retrieval_method"] == "keyword" for r in matches):
                draft["missing_fields"].append(PREFIX + f"select the exact model/options for item {position}")
            continue
        exact_items += 1
        row = matches[0]
        original_name = item.get("product_name") or "item"
        draft["missing_fields"] = [message for message in draft["missing_fields"]
            if not str(message).startswith(f"Exact catalog product match for {original_name};")]
        draft["pending_confirmations"] = [message for message in draft.get("pending_confirmations", [])
            if not (str(message).startswith("Double-check this product: ") and original_name in str(message))]
        item["product_name"] = row["product_name"]
        item["product_code"] = item.get("product_code") or row["manufacturer_model_code"] or row["sku"]
        item["unit"] = item.get("unit") or row["unit"]
        item["price_basis"] = item.get("price_basis") or row["unit"]
        if item.get("unit_price") is None and draft.get("currency") in (None, "", row["currency"]):
            draft["currency"] = row["currency"]
            item["unit_price"] = row["unit_price"]
            item["price_source"] = f"Inventory list: {row['source']['file']}, row {row['source']['row']}"
            # The workbook does not identify purchase versus sale prices.
            draft["missing_fields"].append(PREFIX + f"confirm transaction price for item {position}; inventory list price role is unspecified")
        quantity = item.get("shipped_quantity") or item.get("paid_quantity") or item.get("quantity")
        try:
            requested = Decimal(str(quantity)) if quantity is not None else None
        except InvalidOperation:
            requested = None  # The existing numeric validator owns invalid inputs.
        if requested is not None and requested.is_finite() and requested > row["stock_qty"]:
            draft["missing_fields"].append(PREFIX + f"stock shortage for item {position}: {row['stock_qty']} pcs in source inventory")
    if evidence:
        draft["catalog_evidence"] = evidence
    if items and exact_items == len(items):
        draft["product_family"] = "other"
