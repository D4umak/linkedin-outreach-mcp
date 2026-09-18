"""Prospect Intelligence -- tone analysis + pain point extraction.

Analyzes a prospect's profile data (title, company, headline, industry)
combined with ICP campaign data to produce:
1. Tone profile: How the prospect likely communicates
2. Pain points: Prospect-specific pain points inferred from role + ICP
3. Summary: 1-sentence profile summary for prompt injection

Results are cached in contacts.analysis_json for reuse across
invitation, follow-up, and comment generation.

Backend routing: if in backend mode with no local LLM key,
proxies through the backend API (same pattern as voice_analyzer.py).
"""

from __future__ import annotations

from ..textutil import first_name

import logging
import re
from typing import Any

from ..constants import LLM_TIER_FAST
from .llm import LLMClient
from .llm import loads_json_object as parse_json
from .persona_cards import find_matching_persona
from .prompt_loader import get_prompt_temperature, has_prompt, render_prompt

logger = logging.getLogger(__name__)

PROSPECT_ANALYSIS_SYSTEM = """You are an expert B2B sales intelligence analyst. You analyze prospect profiles to extract communication preferences and likely pain points, enabling personalized outreach.

You are precise, analytical, and output structured JSON only."""

PROSPECT_ANALYSIS_PROMPT = """Analyze this prospect for outreach preparation.

## PROSPECT
Name: {prospect_name}
Title: {prospect_title}
Company: {prospect_company}
Headline: {prospect_headline}
Location: {prospect_location}
Industry: {prospect_industry}

## CAMPAIGN ICP CONTEXT
Target: {campaign_target}
Campaign pain points: {icp_pain_points}
Campaign fears: {icp_fears}
Campaign barriers: {icp_barriers}
{persona_card_section}
## TASK
Produce a prospect intelligence brief with:

### 1. TONE PROFILE
Infer the prospect's likely communication style from their title, industry, and seniority:
- formality_level: 1-10 (1=very casual startup founder, 10=very formal C-suite at enterprise)
- industry_jargon: Industry-specific terms they likely use (list, max 5)
- emotional_tone: Their likely emotional register (e.g., "analytical", "passionate", "pragmatic")
- recommended_approach: 1-sentence recommendation on how to match their tone in outreach

### 2. PAIN POINTS
Cross-reference the prospect's role/company/industry with the ICP pain points.
Produce 2-4 prospect-SPECIFIC pain points they likely face (not generic business platitudes).

### 3. SUMMARY
One sentence capturing who this person is and what they care about.

## OUTPUT
Return ONLY valid JSON (no markdown, no code fences):
{{
    "tone": {{
        "formality_level": 7,
        "industry_jargon": ["term1", "term2"],
        "emotional_tone": "analytical and data-driven",
        "recommended_approach": "Use concise, metrics-oriented language..."
    }},
    "pain_points": [
        "Specific pain point 1 for this prospect's role",
        "Specific pain point 2"
    ],
    "summary": "One-sentence prospect profile summary"
}}"""


_DATA_GAP_RE = re.compile(
    r"(unspecified|insufficient (?:prospect |profile )?information|"
    r"no verified (?:company|role)|insufficient profile data|"
    r"not provided, so no|aren't clear yet|no role-specific)",
    re.I,
)


def is_data_gap_analysis(analysis: dict[str, Any] | None) -> bool:
    """True when cached analysis is a 'we don't know them' disclaimer."""
    if not isinstance(analysis, dict) or not analysis:
        return False
    tone = analysis.get("tone") or {}
    blob = " ".join(
        [
            str(analysis.get("summary") or ""),
            " ".join(str(p) for p in (analysis.get("pain_points") or [])),
            str(tone.get("recommended_approach") or ""),
            str(tone.get("emotional_tone") or ""),
        ]
    )
    return bool(_DATA_GAP_RE.search(blob))


def _fmt_list(items: Any) -> str:
    """Format a list or string for prompt injection."""
    if isinstance(items, list):
        return "; ".join(str(i) for i in items[:5]) if items else "Not specified"
    return str(items) if items else "Not specified"


def _build_deep_profile_section(prospect: dict[str, Any]) -> str:
    """Build a deep profile enrichment section from education, certs, publications, etc.

    Extracts personalization hooks like shared alma mater, recent certifications,
    publications, languages, and volunteer work.
    """
    sections: list[str] = []

    # Education
    education = prospect.get("education") or []
    if isinstance(education, list) and education:
        edu_items: list[str] = []
        for ed in education[:3]:
            if isinstance(ed, dict):
                school = ed.get("school") or ed.get("school_name") or ed.get("institution") or ""
                degree = ed.get("degree") or ed.get("degree_name") or ""
                field = ed.get("field_of_study") or ed.get("field") or ""
                if school:
                    parts = [school]
                    if degree:
                        parts.append(degree)
                    if field:
                        parts.append(field)
                    edu_items.append(" — ".join(parts))
            elif isinstance(ed, str) and ed:
                edu_items.append(ed)
        if edu_items:
            sections.append("Education: " + "; ".join(edu_items))

    # Certifications
    certs = prospect.get("certifications") or []
    if isinstance(certs, list) and certs:
        cert_names: list[str] = []
        for c in certs[:4]:
            if isinstance(c, dict):
                name = c.get("name") or c.get("title") or ""
                authority = c.get("authority") or c.get("issuing_organization") or ""
                if name:
                    cert_names.append(f"{name}" + (f" ({authority})" if authority else ""))
            elif isinstance(c, str) and c:
                cert_names.append(c)
        if cert_names:
            sections.append("Certifications: " + "; ".join(cert_names))

    # Publications
    pubs = prospect.get("publications") or []
    if isinstance(pubs, list) and pubs:
        pub_titles: list[str] = []
        for p in pubs[:3]:
            if isinstance(p, dict):
                title = p.get("title") or p.get("name") or ""
                if title:
                    pub_titles.append(title)
            elif isinstance(p, str) and p:
                pub_titles.append(p)
        if pub_titles:
            sections.append("Publications: " + "; ".join(pub_titles))

    # Languages
    langs = prospect.get("languages") or []
    if isinstance(langs, list) and langs:
        lang_names: list[str] = []
        for lang in langs[:5]:
            if isinstance(lang, dict):
                name = lang.get("name") or lang.get("language") or ""
                proficiency = lang.get("proficiency") or ""
                if name:
                    lang_names.append(f"{name}" + (f" ({proficiency})" if proficiency else ""))
            elif isinstance(lang, str) and lang:
                lang_names.append(lang)
        if lang_names:
            sections.append("Languages: " + ", ".join(lang_names))

    # Volunteer experience
    volunteer = prospect.get("volunteer") or []
    if isinstance(volunteer, list) and volunteer:
        vol_items: list[str] = []
        for v in volunteer[:2]:
            if isinstance(v, dict):
                role = v.get("role") or v.get("title") or ""
                org = v.get("organization") or v.get("company") or ""
                if role or org:
                    vol_items.append(f"{role}" + (f" at {org}" if org else ""))
            elif isinstance(v, str) and v:
                vol_items.append(v)
        if vol_items:
            sections.append("Volunteer: " + "; ".join(vol_items))

    # Honors/Awards
    honors = prospect.get("honors") or []
    if isinstance(honors, list) and honors:
        honor_names: list[str] = []
        for h in honors[:3]:
            if isinstance(h, dict):
                title = h.get("title") or h.get("name") or ""
                if title:
                    honor_names.append(title)
            elif isinstance(h, str) and h:
                honor_names.append(h)
        if honor_names:
            sections.append("Honors: " + "; ".join(honor_names))

    # Skills (top skills give insight into expertise)
    skills = prospect.get("skills") or []
    if isinstance(skills, list) and len(skills) > 5:
        sections.append("Top skills: " + ", ".join(str(s) for s in skills[:8]))

    about = (prospect.get("summary") or prospect.get("about") or "").strip()
    if about:
        sections.insert(0, "About: " + about[:400])

    experience = prospect.get("experience") or []
    if isinstance(experience, list) and experience:
        from ..linkedin.experience import format_experience
        excerpt = format_experience(experience[:3])
        if excerpt and excerpt != "No experience data available":
            sections.insert(1 if about else 0, "Experience:\n" + excerpt)

    if not sections:
        return ""

    return (
        "\n## DEEP PROFILE DATA (use for personalization hooks)\n"
        + "\n".join(f"- {s}" for s in sections)
        + "\nLook for shared background (alma mater, certifications, interests) "
        "and expertise signals for more personalized outreach.\n"
    )


def _default_analysis(prospect: dict[str, Any]) -> dict[str, Any]:
    """Return neutral defaults when analysis fails."""
    title = prospect.get("title", "Professional")
    company = prospect.get("company", "their company")
    return {
        "tone": {
            "formality_level": 5,
            "industry_jargon": [],
            "emotional_tone": "professional",
            "recommended_approach": "Use a neutral, professional tone",
        },
        "pain_points": [],
        "summary": f"{title} at {company}" if title and company else "Professional",
    }


async def analyze_prospect(
    prospect: dict[str, Any],
    campaign_context: dict[str, Any],
    icp_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze a prospect's likely tone and pain points for outreach.

    Args:
        prospect: Prospect profile data (name, title, company, headline, etc.)
        campaign_context: Campaign target description and relevance hook
        icp_data: Parsed ICP JSON with pain_points, fears, barriers

    Returns:
        Dict with "tone" (dict), "pain_points" (list[str]), "summary" (str).
    """
    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client
        client = get_linkedin_client()
        try:
            return await client.analyze_prospect(
                prospect=prospect,
                campaign_context=campaign_context,
                icp_data=icp_data,
            )
        finally:
            await client.close()

    icp = icp_data or {}

    # Get prospect's first name for prompt
    prospect_name = first_name(prospect.get("name"), "Unknown")

    # Look up matching persona card from KB
    persona = find_matching_persona(prospect.get("title", ""))
    persona_section = ""
    if persona:
        pains = "\n".join(f"  - {p}" for p in persona["pain_points"][:5])
        kpis = ", ".join(persona["kpis"][:5])
        triggers = ", ".join(persona["triggers"][:4])
        angles = "\n".join(f"  - {a}" for a in persona["messaging_angles"][:4])
        persona_section = (
            f"\n## BUYER PERSONA CARD ({persona['role']})\n"
            f"Known pain points:\n{pains}\n"
            f"KPIs they care about: {kpis}\n"
            f"Buying triggers: {triggers}\n"
            f"Messaging angles:\n{angles}\n"
        )
        logger.info("Matched persona card: %s for title: %s", persona["role"], prospect.get("title", ""))

    # Build hiring intent section (if prospect's company is actively hiring)
    hiring_section = ""
    hiring_intent = prospect.get("hiring_intent", "")
    hiring_jobs = prospect.get("hiring_jobs", [])
    if hiring_intent:
        hiring_section = f"\n## HIRING INTENT SIGNAL\n{hiring_intent}\n"
        if hiring_jobs:
            for hj in hiring_jobs[:3]:
                hiring_section += f"  - {hj.get('title', '')}\n"
        hiring_section += (
            "Use this signal in your analysis — the prospect's company is "
            "actively investing in this area, which indicates budget and urgency.\n"
        )

    # Build deep profile section (education, certs, publications, languages)
    deep_profile_section = _build_deep_profile_section(prospect)

    # Build template variables
    tpl_vars = {
        "prospect_name": prospect_name,
        "prospect_title": prospect.get("title", ""),
        "prospect_company": prospect.get("company", ""),
        "prospect_headline": prospect.get("headline", ""),
        "prospect_location": prospect.get("location", ""),
        "prospect_industry": prospect.get("industry", ""),
        "campaign_target": campaign_context.get("target_description", ""),
        "icp_pain_points": _fmt_list(icp.get("pain_points", [])),
        "icp_fears": _fmt_list(icp.get("fears", [])),
        "icp_barriers": _fmt_list(icp.get("barriers", [])),
        "persona_card_section": persona_section + hiring_section + deep_profile_section,
    }

    # v63 path: use JSON prompt template
    if has_prompt("prospect_analysis"):
        logger.debug("Using v63 prospect_analysis prompt")
        prompt = render_prompt("prospect_analysis", tpl_vars)
        temperature = get_prompt_temperature("prospect_analysis")
    else:
        logger.debug("Using legacy prospect_analysis prompt")
        prompt = PROSPECT_ANALYSIS_PROMPT.format(**tpl_vars)
        temperature = 0.5

    llm_client = LLMClient()
    raw = await llm_client.generate(
        prompt, system=PROSPECT_ANALYSIS_SYSTEM, temperature=temperature,
        tier=LLM_TIER_FAST,
    )

    # Parse JSON with 2-tier repair
    fallback = _default_analysis(prospect)
    result = parse_json(raw, fallback=fallback)

    # Ensure all expected keys are present
    if "tone" not in result:
        result["tone"] = fallback["tone"]
    if "pain_points" not in result:
        result["pain_points"] = fallback["pain_points"]
    if "summary" not in result:
        result["summary"] = fallback["summary"]

    # Enrich with persona card data if LLM output is thin
    if persona:
        result["persona_match"] = persona["role"]
        result["messaging_angles"] = persona["messaging_angles"][:4]
        result["buying_triggers"] = persona["triggers"][:4]
        result["value_drivers"] = persona.get("value_drivers", [])
        # Supplement pain points if LLM returned fewer than 2
        llm_pains = result.get("pain_points", [])
        if len(llm_pains) < 2:
            result["pain_points"] = persona["pain_points"][:4]

    # Enrich with deep profile personalization hooks
    education = prospect.get("education") or []
    if isinstance(education, list) and education:
        schools: list[str] = []
        for ed in education[:2]:
            if isinstance(ed, dict):
                school = ed.get("school") or ed.get("school_name") or ""
                if school:
                    schools.append(school)
            elif isinstance(ed, str):
                schools.append(ed)
        if schools:
            result["education_hooks"] = schools

    certs = prospect.get("certifications") or []
    if isinstance(certs, list) and certs:
        cert_names: list[str] = []
        for c in certs[:3]:
            if isinstance(c, dict):
                name = c.get("name") or c.get("title") or ""
                if name:
                    cert_names.append(name)
            elif isinstance(c, str):
                cert_names.append(c)
        if cert_names:
            result["certification_hooks"] = cert_names

    pubs = prospect.get("publications") or []
    if isinstance(pubs, list) and pubs:
        result["has_publications"] = True

    return result
