"""Full ICP generation pipeline with RAG.

Sequential async pipeline (no LangGraph dependency):
  1. Ingest: crawl URL / chunk text
  2. Embed: compute and store embeddings
  3. Retrieve: vector search for relevant chunks
  3b. Retrieve KB: sales-methodology cards from the shipped corpus
  4. Summarize: LLM-compress evidence
  5. Synthesize: LLM generates 2-4 ICPs with evidence context
  6. Cite & Score: attach citations, compute confidence
  7. Enrich: resolve LinkedIn params to provider-specific codes

Ported from the original AI in Charge ICP Agent (LangGraph 6-node pipeline).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..db.async_bridge import run_db
from .context_summarizer import CompressedEvidence, compress_evidence, summarize_context
from .icp_schemas import IcpResult, SingleIcp
from .vector_search import SearchResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """State passed through the pipeline."""

    # Input
    target_description: str = ""
    company_context: str = ""       # URL or text
    focus_query: str = ""
    user_profile: dict[str, Any] = field(default_factory=dict)
    confidence_threshold: float = 0.4
    # One of heylead.goals.VALID_GOALS: whose profile this is (#1153).
    goal: str = "sell"

    # Intermediate results
    icp_id: str = ""                # DB record; created by _step_synthesize
    source_id: str = ""
    client_chunks: list[SearchResult] = field(default_factory=list)
    client_summary: str = ""
    evidence_snippets: list[CompressedEvidence] = field(default_factory=list)
    kb_chunks: list[SearchResult] = field(default_factory=list)

    # Output
    result: IcpResult | None = None
    processing_time: float = 0.0
    status: str = "pending"         # pending | processing | success | error
    error_message: str | None = None


async def run_icp_pipeline(
    target_description: str,
    company_context: str = "",
    focus_query: str = "",
    user_profile: dict[str, Any] | None = None,
    confidence_threshold: float = 0.4,
    goal: str = "sell",
) -> IcpResult:
    """Execute the full ICP generation pipeline.

    Args:
        target_description: Natural language target (e.g., "CTOs at fintech startups")
        company_context: URL or text about the company
        focus_query: Optional focus (e.g., "enterprise segment only")
        user_profile: Sender's profile + expertise
        confidence_threshold: Min confidence to include an ICP
        goal: One of heylead.goals.VALID_GOALS; passed to the generator (#1153)

    Returns:
        IcpResult with 2-4 evidence-grounded ICPs.
    """
    start_time = time.time()
    state = PipelineState(
        target_description=target_description,
        company_context=company_context,
        focus_query=focus_query,
        user_profile=user_profile or {},
        confidence_threshold=confidence_threshold,
        goal=goal,
    )

    try:
        state.status = "processing"

        # Step 1: Ingest company data
        await _step_ingest(state)

        # Step 2: Embed chunks
        await _step_embed(state)

        # Step 3: Retrieve relevant chunks
        await _step_retrieve(state)

        # Step 3b: Retrieve sales-methodology evidence (always, no crawl needed)
        await _step_retrieve_kb(state)

        # Step 4: Summarize evidence
        await _step_summarize(state)

        # Step 5: Synthesize ICPs
        await _step_synthesize(state)

        # Step 6: Apply evidence citations and confidence scores
        await _step_cite_and_score(state)

        # Step 7: Enrich LinkedIn parameters with provider codes
        await _step_enrich_linkedin(state)

        state.status = "success"

    except Exception as e:
        state.status = "error"
        state.error_message = str(e)
        logger.error(f"ICP pipeline failed: {e}", exc_info=True)

        # Build fallback result. Nothing was synthesized, so the draft row
        # holds only the '{}' placeholder — drop it rather than leave behind
        # something a later reader will take for a generated ICP.
        if state.result is None:
            if state.icp_id:
                try:
                    from ..db.queries import delete_icp
                    await run_db(delete_icp, state.icp_id)
                except Exception:
                    logger.warning("Could not drop the draft ICP row", exc_info=True)
                state.icp_id = ""
            from .icp_generator_v2 import _build_fallback
            state.result = _build_fallback(target_description)

    state.processing_time = time.time() - start_time
    if state.result:
        state.result.processing_time = state.processing_time

    return state.result  # type: ignore[return-value]


# ──────────────────────────────────────────────
# Pipeline Steps
# ──────────────────────────────────────────────

async def _step_ingest(state: PipelineState) -> None:
    """Step 1: Ingest company data (crawl URL or chunk text)."""
    if not state.company_context:
        logger.info("No company context provided, skipping ingestion")
        return

    from ..config import get_firecrawl_api_key
    from .ingestion.chunker import chunk_pages, chunk_text
    from .ingestion.crawler import crawl_website
    from .ingestion.store import save_chunks, save_source

    is_url = state.company_context.strip().startswith("http")

    # 8 Sep 2026: a placeholder icp_json is indistinguishable downstream from
    # a real, empty ICP — one such placeholder was pulled, pushed back and
    # blanked a live campaign's ICP on the backend. The row cannot simply be
    # deferred: icp_sources.icp_id is a FOREIGN KEY and icps.icp_json is NOT
    # NULL, so ingestion has to create something.
    #
    # What it must not create is a row that reads as a finished ICP. It is
    # stamped status='draft' — no consumer of list_icps(status='active') can
    # see it — and _step_synthesize is the only writer that promotes it to
    # 'active' with real content. A run that never synthesises deletes it.
    from ..db.queries import save_icp, update_icp

    state.icp_id = await run_db(
        save_icp,
        name=f"ICP: {state.target_description[:40]}",
        icp_json="{}",
        target_desc=state.target_description,
        source_url=state.company_context if is_url else "",
    )
    await run_db(update_icp, state.icp_id, status="draft")

    if is_url:
        # Crawl website
        api_key = get_firecrawl_api_key()
        pages = await crawl_website(
            state.company_context, max_pages=10, firecrawl_api_key=api_key,
        )
        if not pages:
            logger.warning(f"No content crawled from {state.company_context}")
            return

        # Create source record
        content_hash = hashlib.md5(
            "".join(p.get("markdown", "") for p in pages).encode()
        ).hexdigest()
        state.source_id = await run_db(
            save_source,
            icp_id=state.icp_id,
            source_type="website",
            uri=state.company_context,
            title=pages[0].get("title", ""),
            content_hash=content_hash,
        )

        # Chunk pages
        chunks = chunk_pages(pages)
    else:
        # Plain text
        content_hash = hashlib.md5(state.company_context.encode()).hexdigest()
        state.source_id = await run_db(
            save_source,
            icp_id=state.icp_id,
            source_type="text",
            uri="",
            title="Company context (text)",
            content_hash=content_hash,
        )

        chunks = chunk_text(
            state.company_context,
            source_uri="user-provided",
            source_title="Company context",
        )

    if chunks:
        await run_db(save_chunks, state.source_id, chunks)
        logger.info(f"Ingested {len(chunks)} chunks")


async def _step_embed(state: PipelineState) -> None:
    """Step 2: Compute embeddings for ingested chunks."""
    if not state.source_id:
        return

    try:
        from .embeddings import _check_icp_deps, embeddings_to_bytes, get_embedder
        _check_icp_deps()
    except RuntimeError:
        logger.info("Embedding deps not installed, skipping embedding step")
        return

    from .ingestion.store import get_chunks_for_source, save_chunk_embeddings

    chunks = await run_db(get_chunks_for_source, state.source_id)
    if not chunks:
        return

    embedder = get_embedder()
    texts = [c["text"] for c in chunks]
    chunk_ids = [c["id"] for c in chunks]

    logger.info(f"Computing embeddings for {len(texts)} chunks...")
    embeddings = embedder.embed(texts)
    emb_bytes = embeddings_to_bytes(embeddings)

    await run_db(save_chunk_embeddings, state.source_id, chunk_ids, emb_bytes)
    logger.info(f"Embeddings saved for {len(chunk_ids)} chunks")


async def _step_retrieve(state: PipelineState) -> None:
    """Step 3: Retrieve relevant chunks via vector search."""
    if not state.source_id:
        return

    try:
        from .embeddings import _check_icp_deps
        _check_icp_deps()
    except RuntimeError:
        return

    from .vector_search import search_chunks

    query = state.target_description
    if state.focus_query:
        query = f"{query} {state.focus_query}"

    state.client_chunks = await search_chunks(
        query=query,
        source_id=state.source_id,
        top_k=20,
    )
    logger.info(f"Retrieved {len(state.client_chunks)} relevant chunks")


async def _step_retrieve_kb(state: PipelineState) -> None:
    """Step 3b: Retrieve sales-methodology evidence from the shipped KB.

    9 Sep 2026. `PipelineState.kb_chunks` has existed since the
    pipeline was ported and was never written by anything, so
    `apply_evidence_to_icp` has always been called with `kb_chunk_count=0`
    and the README's "RAG-powered buyer personas" was true only when the
    seller's own website happened to be crawled. This step runs on EVERY
    path — no website, no crawl, no embedding extra required.
    """
    from .kb_retrieval import personas_for_titles, retrieve_kb
    from .vector_search import SearchResult

    query = state.target_description
    if state.focus_query:
        query = f"{query} {state.focus_query}"

    personas: list[str] = []
    if state.result is not None:
        titles: list[str] = []
        for icp in state.result.icps:
            titles.extend(icp.job_titles.include[:6])
        personas = personas_for_titles(titles)

    cards = retrieve_kb(query, personas=personas or None, top_k=8)
    state.kb_chunks = [
        SearchResult(
            chunk_id=card.id,
            text=card.as_evidence_line(),
            ctx_text=", ".join(card.pain_symptoms),
            header_path=f"{'/'.join(card.personas)} / {card.stage}",
            source_uri=f"kb://{card.id}",
            source_title=card.source,
            similarity_score=card.score,
            position=i,
        )
        for i, card in enumerate(cards)
    ]
    logger.info("Retrieved %d KB cards for ICP synthesis", len(state.kb_chunks))


async def _step_summarize(state: PipelineState) -> None:
    """Step 4: Compress and summarize evidence."""
    if not state.client_chunks:
        return

    state.evidence_snippets = await compress_evidence(
        query=state.target_description,
        search_results=state.client_chunks,
        max_snippets=10,
    )

    if state.evidence_snippets:
        state.client_summary = await summarize_context(state.evidence_snippets)
        logger.info(
            f"Summarized {len(state.evidence_snippets)} evidence snippets "
            f"into {len(state.client_summary)} chars"
        )


async def _step_synthesize(state: PipelineState) -> None:
    """Step 5: Generate ICPs using evidence context."""
    from .icp_generator_v2 import generate_icp_v2

    # If we have evidence, pass it as company context
    enriched_context = state.company_context
    if state.client_summary:
        enriched_context = (
            f"## BUSINESS CONTEXT (from company research)\n"
            f"{state.client_summary}\n\n"
            f"## EVIDENCE SNIPPETS\n"
        )
        for e in state.evidence_snippets[:8]:
            source = e.source_title or e.source_uri or "Unknown"
            enriched_context += f"- [{source}] {e.evidence_snippet}\n"

    state.result = await generate_icp_v2(
        target_description=state.target_description,
        company_context=enriched_context,
        focus_query=state.focus_query,
        user_profile=state.user_profile,
        confidence_threshold=state.confidence_threshold,
        kb_evidence=[c.text for c in state.kb_chunks],
        goal=state.goal,
    )

    # Store source info
    if state.result:
        state.result.source_info = {
            "icp_id": state.icp_id,
            "source_id": state.source_id,
            "chunks_ingested": len(state.client_chunks),
            "evidence_count": len(state.evidence_snippets),
            "has_summary": bool(state.client_summary),
            "kb_chunks": len(state.kb_chunks),
        }

    # Promote the draft row: this is the first moment there is anything real
    # to store, and the only writer that sets status='active'.
    if state.icp_id and state.result:
        from ..db.queries import update_icp
        from .icp_schemas import icp_result_to_json

        best_confidence = max(
            (icp.overall_confidence for icp in state.result.icps), default=0.5,
        )
        await run_db(
            update_icp,
            state.icp_id,
            name=state.result.campaign_name or state.target_description[:40],
            icp_json=icp_result_to_json(state.result),
            confidence=best_confidence,
            status="active",
        )


async def _step_cite_and_score(state: PipelineState) -> None:
    """Step 6: Apply evidence citations and confidence scores to ICPs."""
    # KB evidence alone is enough to score: before 9 Sep 2026 this returned
    # early on every campaign without a crawled website, which is most of them.
    if not state.result or not (state.evidence_snippets or state.kb_chunks):
        return

    from .evidence import apply_evidence_to_icp

    for icp in state.result.icps:
        apply_evidence_to_icp(
            icp,
            evidence_snippets=state.evidence_snippets,
            kb_results=state.kb_chunks or None,
            client_chunk_count=len(state.client_chunks),
            kb_chunk_count=len(state.kb_chunks),
        )

    # Re-save the ICP record with updated confidence scores
    if state.icp_id:
        from ..db.queries import update_icp
        from .icp_schemas import icp_result_to_json

        best_confidence = max(
            (icp.overall_confidence for icp in state.result.icps), default=0.5,
        )
        await run_db(
            update_icp,
            state.icp_id,
            icp_json=icp_result_to_json(state.result),
            confidence=best_confidence,
        )


async def _step_enrich_linkedin(state: PipelineState) -> None:
    """Step 7: Enrich ICP LinkedIn parameters with provider-specific codes."""
    if not state.result or not state.result.icps:
        return

    from ..db.queries import get_setting
    from ..linkedin import get_linkedin_client
    from .linkedin_enricher import enrich_icp_linkedin_params

    account_id = await run_db(get_setting, "unipile_account_id")
    if not account_id:
        logger.info("No LinkedIn account — skipping enrichment")
        return

    try:
        client = get_linkedin_client()
    except Exception as e:
        logger.warning(f"Cannot create LinkedIn client for enrichment: {e}")
        return

    # Build the get_params_fn closure based on client type
    from ..linkedin.backend_client import BackendClient

    if isinstance(client, BackendClient):
        async def get_params_fn(type: str, keywords: str) -> list[dict[str, str]]:
            return await client.get_search_params(type=type, keywords=keywords)
    else:
        async def get_params_fn(type: str, keywords: str) -> list[dict[str, str]]:
            return await client.get_search_params(
                account_id=account_id, type=type, keywords=keywords,
            )

    enriched_count = 0
    for icp in state.result.icps:
        try:
            icp.linkedin_enriched_params = await enrich_icp_linkedin_params(
                icp, get_params_fn,
            )
            enriched_count += 1
        except Exception as e:
            logger.warning(f"Enrichment failed for ICP '{icp.name}': {e}")

    if enriched_count:
        logger.info(f"Enriched LinkedIn params for {enriched_count}/{len(state.result.icps)} ICPs")

    # Final save with enrichment data
    if state.icp_id and enriched_count:
        from ..db.queries import update_icp
        from .icp_schemas import icp_result_to_json

        await run_db(
            update_icp,
            state.icp_id,
            icp_json=icp_result_to_json(state.result),
        )
