"""LLM prompts for the autonomous strategy engine.

Separate from experiment_service prompts — these focus on cross-campaign
pattern detection and revenue-optimized strategy recommendations.
"""

from __future__ import annotations

STRATEGY_ANALYSIS_SYSTEM = """You are an autonomous LinkedIn outreach strategy optimizer.
You analyze cross-campaign performance data to identify actionable patterns
that maximize ESTIMATED DEAL REVENUE (not just reply rate or volume).

Your primary optimization target: estimated_revenue × conversion_probability

Key benchmarks:
- Acceptance rate: 20-30% good, >35% excellent, <15% needs work
- Reply rate: 10-20% of connected is healthy
- Conversion rate (won/total closed): >30% is good, >50% is excellent
- Time to accept: 2-5 days typical; <2 days = high intent
- Time to reply: 1-3 days typical

Revenue signals:
- Higher seniority (VP, C-level) = higher deal value but lower volume
- Enterprise (1000+ employees) = higher ACV but longer cycles
- Mid-market (200-1000) = best revenue/effort ratio typically

Your recommendations must be:
1. SPECIFIC — reference actual numbers from the data
2. ACTIONABLE — describe exactly what to change
3. REVENUE-FOCUSED — every recommendation should tie back to revenue impact
4. EVIDENCE-BASED — cite sample sizes and statistical significance"""


PATTERN_DETECTION_PROMPT = """Analyze this cross-campaign performance data and identify revenue-maximizing patterns.

## Segment Performance (industry + title groupings)
{segment_data}

## Engagement Strategy Correlation
{engagement_data}

## Timing Heatmap (day-of-week × hour → acceptance rate)
{timing_data}

## Fit Score vs Outcomes
{fit_score_data}

## Won Deal Profiles (top converters)
{won_profiles}

## Lost Deal Profiles (non-converters)
{lost_profiles}

## Revenue-Weighted Funnel
{revenue_funnel}

---

Identify 2-5 patterns. For each pattern:

Return ONLY valid JSON:
{{
    "patterns": [
        {{
            "pattern_type": "icp_segment | messaging_tone | engagement_tactic | timing | revenue_segment",
            "pattern_key": "unique_key (e.g. fintech+VP+51-200)",
            "description": "what you observed",
            "evidence": "specific numbers from the data above",
            "confidence": 0.0-1.0,
            "estimated_revenue_impact": dollar_amount_per_prospect,
            "recommended_action": "what to do about it"
        }}
    ],
    "spawn_candidates": [
        {{
            "segment_description": "natural language description",
            "target_description": "description suitable for create_campaign (e.g. 'VPs of Engineering at fintech companies with 51-200 employees')",
            "estimated_revenue_per_prospect": dollar_amount,
            "confidence": 0.0-1.0,
            "reasoning": "why this segment should get its own campaign"
        }}
    ],
    "campaign_adjustments": [
        {{
            "campaign_name": "name of the campaign to adjust",
            "action": "skip_segment | adjust_messaging | shift_budget | reorder_queue",
            "details": "specific change to make",
            "expected_impact": "what improvement to expect"
        }}
    ]
}}"""


MESSAGING_ADJUSTMENT_PROMPT = """Given these campaign results for the segment "{segment_description}":

Acceptance rate: {acceptance_rate}%
Reply rate: {reply_rate}%
Average estimated revenue: ${avg_revenue}
Current messaging approach: {current_approach}
Won deals common traits: {won_traits}
Lost deals common traits: {lost_traits}

Recommend a specific messaging adjustment to maximize replies from high-revenue prospects.

Return ONLY valid JSON:
{{
    "recommended_approach": "1-2 sentence messaging strategy",
    "tone": "formal | conversational | direct | consultative",
    "lead_with": "pain_point | question | social_proof | direct_value | mutual_connection",
    "avoid": "what NOT to do based on lost deal patterns",
    "reasoning": "why this adjustment should work"
}}"""
