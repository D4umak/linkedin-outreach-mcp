"""Brand strategist — LLM-powered LinkedIn personal brand analysis & planning.

Audits the user's LinkedIn profile, generates a 4-week brand strategy,
and produces specific action outputs (headline rewrites, post topics, etc.).

Routes through the backend proxy when in backend mode (no local LLM key).
"""

from __future__ import annotations

import logging
from typing import Any

from .llm import LLMClient
from .llm import loads_json_object as parse_json

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Prompt: Brand Profile Analysis
# ──────────────────────────────────────────────

_ANALYSIS_SYSTEM = (
    "You are a LinkedIn personal branding expert and social selling strategist. "
    "You analyze LinkedIn profiles to identify strengths, weaknesses, and specific "
    "improvement opportunities that drive more inbound leads and improve outreach "
    "acceptance rates. You are precise, actionable, and output structured JSON only."
)

_ANALYSIS_PROMPT = """Analyze this LinkedIn profile for personal brand optimization.

## PROFILE DATA
Name: {name}
Headline: {headline}
Title: {title} at {company}
Location: {location}
Industry: {industry}
Summary: {summary}
Connections: {connections}

## EXPERIENCE
{experience}

## SKILLS
{skills}

## RECENT POSTS ({post_count} posts)
{posts}

## CURRENT METRICS
SSI Score: {ssi_score}/100
SSI Pillars: {ssi_pillars}
Campaign Acceptance Rate: {acceptance_rate}
Campaign Reply Rate: {reply_rate}

## VOICE ANALYSIS
Tone: {voice_tone}
Expertise: {expertise}

## TARGET AUDIENCE (from active campaigns)
{icp_context}

## TASK
Perform a comprehensive brand audit across 6 areas. Score each 0-10.
If target audience data is provided, evaluate how well the profile appeals to
that specific audience. The headline, summary, and content should resonate with
the target titles and address their pain points.

### 1. HEADLINE (score 0-10)
- Is it clear what they do and who they help?
- Does it contain relevant keywords for LinkedIn search?
- Is it compelling enough to drive profile clicks?
- Suggest an improved headline

### 2. SUMMARY (score 0-10)
- Does it tell a story or just list responsibilities?
- Does it have a clear CTA?
- Is it optimized for LinkedIn search with keywords?
- Does it demonstrate credibility (numbers, results)?

### 3. CONTENT (score 0-10)
- Posting frequency vs. benchmark (2-3x/week optimal)
- Topic diversity and relevance to target audience
- Post quality and engagement potential
- Content type variety (text, stories, questions, polls)

### 4. PROFILE COMPLETENESS (score 0-10)
- Check: photo, banner, headline, about, experience, skills, education, featured, recommendations, open-to-work badge, profile picture quality
- List what's missing
- For education: note if education section is empty or incomplete
- For open-to-work: note if it could help attract inbound connections

### 5. SSI IMPROVEMENT (score based on SSI data)
- Specific actions for each of the 4 SSI pillars:
  * Establish your professional brand
  * Find the right people
  * Engage with insights
  * Build relationships
- Priority based on weakest pillars

### 6. ENGAGEMENT (score 0-10)
- Are they actively commenting on others' posts?
- Network growth strategy
- Thought leadership signals

## RESPONSE FORMAT
Return ONLY valid JSON (no markdown, no explanation):
{{
    "overall_score": 72,
    "areas": {{
        "headline": {{
            "score": 5,
            "current": "the current headline text",
            "issues": ["issue1", "issue2"],
            "suggestion": "improved headline text"
        }},
        "summary": {{
            "score": 6,
            "issues": ["issue1", "issue2"],
            "suggestion": "brief description of what to improve"
        }},
        "content": {{
            "score": 3,
            "posting_frequency": "X posts in recent period",
            "issues": ["issue1", "issue2"]
        }},
        "profile_completeness": {{
            "score": 7,
            "missing": ["section1", "section2"],
            "present": ["section1", "section2"]
        }},
        "ssi_breakdown": {{
            "score": 45,
            "improvements": ["action for weakest pillar", "action2", "action3"]
        }},
        "engagement": {{
            "score": 4,
            "issues": ["issue1", "issue2"]
        }}
    }},
    "top_3_actions": ["most impactful action first", "second action", "third action"]
}}"""


# ──────────────────────────────────────────────
# Prompt: Brand Strategy Plan
# ──────────────────────────────────────────────

_PLAN_SYSTEM = (
    "You are a LinkedIn content strategist. You create actionable 4-week personal "
    "brand improvement plans that increase Social Selling Index, acceptance rates, "
    "and inbound engagement. Plans must be specific, executable, and tailored to "
    "the person's industry and expertise. Output structured JSON only."
)

_PLAN_PROMPT = """Create a 4-week personal brand strategy for this LinkedIn professional.

## PROFILE
Name: {name}
Title: {title} at {company}
Industry: {industry}
Expertise: {expertise}
Voice Tone: {voice_tone}

## BRAND AUDIT RESULTS
Overall Score: {overall_score}/100
Headline Score: {headline_score}/10 — Issues: {headline_issues}
Summary Score: {summary_score}/10 — Issues: {summary_issues}
Content Score: {content_score}/10 — Issues: {content_issues}
Completeness Score: {completeness_score}/10 — Missing: {missing_sections}
SSI Score: {ssi_score}/100 — Improvements: {ssi_improvements}
Engagement Score: {engagement_score}/10 — Issues: {engagement_issues}

## FOCUS AREA
{focus}

## TARGET AUDIENCE (from active campaigns)
{icp_context}

## TASK
Create a 4-week plan with 3-4 actions per week.
If target audience data is provided, all post topics, headline suggestions,
and engagement activities should be tailored to appeal to this audience.
Address their pain points in content. Use their industry language. Each action should be specific
and executable. Include a content calendar with posting days and topic types.

Action types (ALL are AUTOMATICALLY EXECUTED via LinkedIn API):
- "profile_optimize" with subtype: "headline", "summary", "education", or "open_to_work" (AUTO-APPLIED to LinkedIn profile)
- "photo_enhance" — auto-applies professional photo settings (STUDIO filter, optimal contrast/brightness)
- "post" with a specific topic related to their expertise (AUTO-PUBLISHED to LinkedIn)
- "engagement" with engagement_type: "comment" (AUTO-COMMENTS on trending posts in niche), "react" (AUTO-LIKES posts), or "follow" (AUTO-FOLLOWS relevant people)

All actions are fully automated. Use "profile_optimize" for quick wins in Week 1.
Use "photo_enhance" if the profile audit identified photo quality issues.

Each week should build on the previous. Week 1 = quick wins (post + engage),
Week 2-3 = content momentum (more posts, deeper engagement), Week 4 = scaling.

## RESPONSE FORMAT
Return ONLY valid JSON:
{{
    "weeks": [
        {{
            "week": 1,
            "theme": "Quick Wins & Foundation",
            "actions": [
                {{
                    "id": "w1a1",
                    "type": "profile_optimize",
                    "subtype": "headline",
                    "description": "specific task description",
                    "status": "pending"
                }},
                {{
                    "id": "w1a2",
                    "type": "post",
                    "topic": "specific post topic tied to their expertise",
                    "tone": "thought-leader",
                    "description": "what to post about and why",
                    "status": "pending"
                }},
                {{
                    "id": "w1a3",
                    "type": "engagement",
                    "engagement_type": "comment",
                    "description": "comment on 10 trending posts about [industry topic]",
                    "target_count": 10,
                    "status": "pending"
                }},
                {{
                    "id": "w1a4",
                    "type": "engagement",
                    "engagement_type": "react",
                    "description": "like 15 posts from thought leaders in [industry]",
                    "target_count": 15,
                    "status": "pending"
                }}
            ]
        }}
    ],
    "content_calendar": [
        {{"day": "Monday", "type": "insight", "example_topic": "industry trend or data point"}},
        {{"day": "Wednesday", "type": "story", "example_topic": "customer or personal experience"}},
        {{"day": "Friday", "type": "question", "example_topic": "poll or discussion starter"}}
    ],
    "engagement_targets": {{
        "weekly_posts": 2,
        "daily_comments": 5,
        "daily_reactions": 10
    }}
}}"""


# ──────────────────────────────────────────────
# Prompt: Brand Action Execution
# ──────────────────────────────────────────────

_ACTION_SYSTEM = (
    "You are a LinkedIn personal branding copywriter. You write compelling, "
    "authentic profile content that drives profile views and connection requests. "
    "You match the user's voice and tone. Output structured JSON only."
)

_HEADLINE_ACTION_PROMPT = """Generate 3 optimized LinkedIn headline options for this professional.

## CURRENT HEADLINE
{current_headline}

## PROFILE
Name: {name}
Title: {title} at {company}
Industry: {industry}
Expertise: {expertise}

## VOICE
Tone: {voice_tone}
Formality: {formality_level}/10

## ISSUES WITH CURRENT
{issues}

## TARGET AUDIENCE
{icp_context}

## RULES
- Max 220 characters each
- If target audience data is provided, include keywords they would search for
  and address their primary pain points or desired outcomes
- Include what they do + who they help + their unique edge
- Use keywords their target audience would search
- Be specific, not generic
- Match their voice tone and formality level

Return ONLY valid JSON:
{{
    "options": [
        {{"text": "headline option 1", "rationale": "why this works"}},
        {{"text": "headline option 2", "rationale": "why this works"}},
        {{"text": "headline option 3", "rationale": "why this works"}}
    ]
}}"""

_SUMMARY_ACTION_PROMPT = """Rewrite this LinkedIn About/Summary section.

## CURRENT SUMMARY
{current_summary}

## PROFILE
Name: {name}
Title: {title} at {company}
Industry: {industry}
Expertise: {expertise}

## VOICE
Tone: {voice_tone}
Formality: {formality_level}/10
Patterns: {voice_patterns}

## ISSUES
{issues}

## TARGET AUDIENCE
{icp_context}

## RULES
- 1500-2000 characters (optimal LinkedIn length)
- If target audience data is provided, write the summary to resonate with them.
  Reference their pain points. Show credibility in their industry.
- Hook in the first 2 lines (visible before "see more")
- Tell a story: who you are, what you do, who you help, results you deliver
- Include a clear CTA at the end (book a call, connect, visit website)
- Use short paragraphs and line breaks for readability
- Include relevant keywords naturally
- Match the user's voice and tone

Return ONLY valid JSON:
{{
    "summary": "the full rewritten summary text",
    "hook": "the first 2 lines that appear before see more",
    "cta": "the call-to-action at the end"
}}"""


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _format_experience(experience: list[dict]) -> str:
    if not experience:
        return "No experience data available"
    lines = []
    for exp in experience:
        title = exp.get("title", "Unknown")
        company = exp.get("company", "Unknown")
        desc = exp.get("description", "")
        line = f"- {title} at {company}"
        if desc:
            line += f": {desc[:200]}"
        lines.append(line)
    return "\n".join(lines)


def _format_posts(posts: list[dict]) -> str:
    if not posts:
        return "No recent posts available."
    lines = []
    for i, post in enumerate(posts, 1):
        text = post.get("text", "").strip()
        if text:
            lines.append(f"Post {i}:\n{text[:500]}\n")
    return "\n".join(lines) if lines else "No posts with text content found."


# ──────────────────────────────────────────────
# Core functions
# ──────────────────────────────────────────────


async def analyze_brand_profile(
    profile: dict[str, Any],
    posts: list[dict],
    ssi_data: dict[str, Any],
    campaign_stats: dict[str, Any],
    voice: dict[str, Any],
    expertise: dict[str, Any],
    icp_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full brand profile audit. Returns scored areas with specific issues."""

    # Route through backend if in backend mode and no local LLM key
    from ..config import has_local_llm_key, is_backend_mode
    from ..services.brand_service import format_icp_context

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            return await client.analyze_brand_strategy(
                profile, posts, ssi_data, campaign_stats,
                icp_context=icp_context,
            )
        finally:
            await client.close()

    # Build pillar string
    ssi_pillars = ssi_data.get("pillars", [])
    pillar_str = (
        ", ".join(f"{p.get('name', '?')}: {p.get('score', 0)}" for p in ssi_pillars)
        if ssi_pillars
        else "Not available"
    )

    tpl_vars = {
        "name": profile.get("name", "Unknown"),
        "headline": profile.get("headline", ""),
        "title": profile.get("title", ""),
        "company": profile.get("company", ""),
        "location": profile.get("location", ""),
        "industry": profile.get("industry", ""),
        "summary": profile.get("summary", ""),
        "connections": profile.get("connections", 0),
        "experience": _format_experience(profile.get("experience", [])),
        "skills": ", ".join(profile.get("skills", [])[:15]) if isinstance(profile.get("skills"), list) else "",
        "post_count": str(len(posts)),
        "posts": _format_posts(posts),
        "ssi_score": ssi_data.get("score", 0),
        "ssi_pillars": pillar_str,
        "acceptance_rate": f"{campaign_stats.get('acceptance_rate', 0):.0%}",
        "reply_rate": f"{campaign_stats.get('reply_rate', 0):.0%}",
        "voice_tone": voice.get("tone", "Not analyzed"),
        "expertise": expertise.get("core", "Not analyzed"),
        "icp_context": format_icp_context(icp_context),
    }

    prompt = _ANALYSIS_PROMPT.format(**tpl_vars)

    llm = LLMClient()
    raw = await llm.generate(prompt, system=_ANALYSIS_SYSTEM, temperature=0.4, max_tokens=3000)

    default = {
        "overall_score": 0,
        "areas": {},
        "top_3_actions": ["Could not analyze — check your LLM configuration"],
    }

    return parse_json(raw, fallback=default)


async def generate_brand_plan(
    analysis: dict[str, Any],
    profile: dict[str, Any],
    voice: dict[str, Any],
    expertise: dict[str, Any],
    focus: str = "all",
    icp_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a 4-week brand strategy plan based on analysis results."""

    from ..services.brand_service import format_icp_context

    areas = analysis.get("areas", {})

    tpl_vars = {
        "name": profile.get("name", "Unknown"),
        "title": profile.get("title", ""),
        "company": profile.get("company", ""),
        "industry": profile.get("industry", ""),
        "expertise": expertise.get("core", ""),
        "voice_tone": voice.get("tone", "professional"),
        "overall_score": analysis.get("overall_score", 0),
        "headline_score": areas.get("headline", {}).get("score", 0),
        "headline_issues": ", ".join(areas.get("headline", {}).get("issues", [])),
        "summary_score": areas.get("summary", {}).get("score", 0),
        "summary_issues": ", ".join(areas.get("summary", {}).get("issues", [])),
        "content_score": areas.get("content", {}).get("score", 0),
        "content_issues": ", ".join(areas.get("content", {}).get("issues", [])),
        "completeness_score": areas.get("profile_completeness", {}).get("score", 0),
        "missing_sections": ", ".join(areas.get("profile_completeness", {}).get("missing", [])),
        "ssi_score": areas.get("ssi_breakdown", {}).get("score", 0),
        "ssi_improvements": ", ".join(areas.get("ssi_breakdown", {}).get("improvements", [])),
        "engagement_score": areas.get("engagement", {}).get("score", 0),
        "engagement_issues": ", ".join(areas.get("engagement", {}).get("issues", [])),
        "focus": f"Focus on: {focus}" if focus and focus != "all" else "Improve all areas, prioritize the weakest scores.",
        "icp_context": format_icp_context(icp_context),
    }

    prompt = _PLAN_PROMPT.format(**tpl_vars)

    default = {
        "weeks": [],
        "content_calendar": [],
        "engagement_targets": {"weekly_posts": 2, "daily_comments": 5, "daily_reactions": 10},
    }

    # Route LLM call: backend API or local
    from ..config import has_local_llm_key, is_backend_mode

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            raw = await client.brand_generate(
                prompt, system=_PLAN_SYSTEM, temperature=0.7, max_tokens=4000,
            )
        finally:
            await client.close()
    else:
        llm = LLMClient()
        raw = await llm.generate(prompt, system=_PLAN_SYSTEM, temperature=0.7, max_tokens=4000)

    return parse_json(raw, fallback=default)


def _expertise_core(expertise: dict[str, Any] | str | None, profile: dict[str, Any]) -> str:
    """Voice signatures have no `core`; that lives on the expertise map."""
    if isinstance(expertise, str) and expertise.strip():
        return expertise.strip()
    if isinstance(expertise, dict):
        core = expertise.get("core", "")
        if isinstance(core, list):
            core = ", ".join(str(item) for item in core if item)
        if isinstance(core, str) and core.strip():
            return core.strip()
    return profile.get("headline", "")


async def generate_brand_action(
    action_item: dict[str, Any],
    profile: dict[str, Any],
    voice: dict[str, Any],
    analysis: dict[str, Any],
    icp_context: dict[str, Any] | None = None,
    expertise: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Generate specific brand action output (headline options, summary rewrite, etc.)."""

    from ..services.brand_service import format_icp_context

    subtype = action_item.get("subtype", "headline")
    areas = analysis.get("areas", {})
    icp_str = format_icp_context(icp_context)

    common_vars = {
        "name": profile.get("name", "Unknown"),
        "title": profile.get("title", ""),
        "company": profile.get("company", ""),
        "industry": profile.get("industry", ""),
        "expertise": _expertise_core(expertise, profile),
        "voice_tone": voice.get("tone", "professional"),
        "formality_level": voice.get("formality_level", 5),
        "voice_patterns": ", ".join(voice.get("communication_style", [])[:5]),
        "icp_context": icp_str,
    }

    if subtype == "headline":
        tpl_vars = {
            **common_vars,
            "current_headline": areas.get("headline", {}).get("current", profile.get("headline", "")),
            "issues": ", ".join(areas.get("headline", {}).get("issues", [])),
        }
        prompt = _HEADLINE_ACTION_PROMPT.format(**tpl_vars)
    elif subtype == "summary":
        tpl_vars = {
            **common_vars,
            "current_summary": profile.get("summary", ""),
            "issues": ", ".join(areas.get("summary", {}).get("issues", [])),
        }
        prompt = _SUMMARY_ACTION_PROMPT.format(**tpl_vars)
    else:
        return {"error": f"Unknown action subtype: {subtype}"}

    # Route LLM call: backend API or local
    from ..config import has_local_llm_key, is_backend_mode

    if is_backend_mode() and not has_local_llm_key():
        from ..linkedin import get_linkedin_client

        client = get_linkedin_client()
        try:
            raw = await client.brand_generate(
                prompt, system=_ACTION_SYSTEM, temperature=0.7, max_tokens=2000,
            )
        finally:
            await client.close()
    else:
        llm = LLMClient()
        raw = await llm.generate(prompt, system=_ACTION_SYSTEM, temperature=0.7, max_tokens=2000)

    return parse_json(raw, fallback={"error": "Failed to generate action content"})
