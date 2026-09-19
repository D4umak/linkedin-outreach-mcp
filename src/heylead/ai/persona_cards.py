"""Buyer persona cards — embedded marketing knowledge base.

22 pre-built buyer personas ported from the original AI in Charge KB.
Each persona has pain points, KPIs, buying triggers, messaging angles,
and value drivers. Used to enrich prospect_analyzer.py output when
a prospect's role matches a known persona.

Usage:
    from .persona_cards import find_matching_persona
    persona = find_matching_persona("VP of Sales")
    if persona:
        pain_points = persona["pain_points"]
"""

from __future__ import annotations

import re
from functools import lru_cache
from ..textutil import contains_term

# ──────────────────────────────────────────────
# 22 Buyer Personas
# ──────────────────────────────────────────────

PERSONA_CARDS: list[dict] = [
    {
        "role": "CFO",
        "aliases": ["chief financial officer", "finance director", "vp finance", "head of finance"],
        "pain_points": [
            "Forecasting risk and cash flow uncertainty",
            "Manual reporting processes consuming team bandwidth",
            "Lack of real-time visibility into spend and pipeline",
            "Difficulty proving ROI on technology investments",
            "Compliance and audit preparation overhead",
        ],
        "kpis": ["revenue growth", "gross margin", "burn rate", "CAC payback", "operating expenses"],
        "triggers": ["budget cycles", "board meetings", "new fiscal year", "M&A activity", "IPO prep"],
        "messaging_angles": [
            "Quantify time saved in reporting cycles",
            "Frame as risk reduction, not just cost cutting",
            "Lead with payback period and ROI metrics",
            "Reference audit-readiness and compliance benefits",
        ],
        "value_drivers": ["cost reduction", "risk mitigation", "operational efficiency"],
    },
    {
        "role": "CRO",
        "aliases": ["chief revenue officer", "vp revenue", "head of revenue"],
        "pain_points": [
            "Pipeline velocity too slow to hit targets",
            "Misalignment between sales and marketing on lead quality",
            "Rep ramp time and productivity gaps",
            "Forecast accuracy undermining board confidence",
            "Customer churn eroding expansion revenue",
        ],
        "kpis": ["ARR/MRR growth", "pipeline coverage ratio", "win rate", "sales cycle length", "NRR"],
        "triggers": ["missed quarter", "new territory expansion", "sales team restructure", "competitor wins"],
        "messaging_angles": [
            "Show pipeline acceleration metrics from similar companies",
            "Frame as revenue assurance, not just tooling",
            "Lead with forecast accuracy improvements",
            "Reference competitive displacement stories",
        ],
        "value_drivers": ["revenue acceleration", "forecast accuracy", "rep productivity"],
    },
    {
        "role": "CMO",
        "aliases": ["chief marketing officer", "vp marketing", "head of marketing", "marketing director"],
        "pain_points": [
            "Proving marketing's contribution to pipeline and revenue",
            "Content production not keeping pace with demand",
            "Lead quality disputes with sales team",
            "Attribution across multi-touch journeys",
            "Brand awareness not translating to demand",
        ],
        "kpis": ["MQLs/SQLs", "pipeline contribution", "CAC", "brand awareness", "content engagement"],
        "triggers": ["rebrand", "new product launch", "demand gen overhaul", "agency review"],
        "messaging_angles": [
            "Lead with pipeline attribution clarity",
            "Frame as marketing-sales alignment tool",
            "Show content ROI measurement capabilities",
            "Reference demand gen acceleration stories",
        ],
        "value_drivers": ["pipeline attribution", "demand generation", "brand-to-revenue connection"],
    },
    {
        "role": "CTO",
        "aliases": ["chief technology officer", "vp engineering", "head of engineering", "engineering director"],
        "pain_points": [
            "Technical debt slowing feature delivery",
            "Hiring and retaining engineering talent",
            "Security and compliance requirements growing",
            "Integration complexity across tool sprawl",
            "Balancing innovation with reliability",
        ],
        "kpis": ["deployment frequency", "MTTR", "engineering velocity", "system uptime", "security posture"],
        "triggers": ["major incident", "scale-up hiring", "technology migration", "compliance audit"],
        "messaging_angles": [
            "Lead with developer experience and productivity gains",
            "Frame as tech debt reduction enabler",
            "Show integration simplicity and API-first approach",
            "Reference security and compliance benefits",
        ],
        "value_drivers": ["developer productivity", "system reliability", "security posture"],
    },
    {
        "role": "CEO/Founder",
        "aliases": ["ceo", "founder", "co-founder", "managing director", "president", "general manager"],
        "pain_points": [
            "Growth stalling after initial traction",
            "Difficulty scaling processes that worked at smaller size",
            "Board pressure on metrics and milestones",
            "Talent acquisition and retention in competitive market",
            "Cash runway management and fundraising timing",
        ],
        "kpis": ["revenue growth rate", "runway months", "team size growth", "market share", "customer count"],
        "triggers": ["fundraising round", "board meeting", "competitor funding", "key hire departure"],
        "messaging_angles": [
            "Frame as growth unlock, not just efficiency",
            "Lead with founder-to-founder credibility",
            "Show how similar-stage companies scaled past the same bottleneck",
            "Keep concise — founders are time-poor",
        ],
        "value_drivers": ["growth acceleration", "operational scale", "competitive advantage"],
    },
    {
        "role": "VP Sales",
        "aliases": ["vp of sales", "head of sales", "sales director", "director of sales"],
        "pain_points": [
            "Reps spending too much time on non-selling activities",
            "Pipeline generation falling behind targets",
            "New rep ramp time too long",
            "Inconsistent messaging across the team",
            "CRM data quality issues undermining forecasting",
        ],
        "kpis": ["quota attainment", "pipeline generation", "average deal size", "ramp time", "activity metrics"],
        "triggers": ["new quota cycle", "team expansion", "CRM migration", "missed targets"],
        "messaging_angles": [
            "Lead with time-back-to-selling metrics",
            "Show ramp time reduction for new reps",
            "Frame as pipeline generation multiplier",
            "Reference messaging consistency benefits",
        ],
        "value_drivers": ["rep productivity", "pipeline generation", "quota attainment"],
    },
    {
        "role": "COO/Head of Ops",
        "aliases": ["coo", "chief operating officer", "vp operations", "head of operations", "director of operations"],
        "pain_points": [
            "Process bottlenecks slowing cross-team execution",
            "Lack of visibility into operational metrics",
            "Manual handoffs between departments causing delays",
            "Scaling operations without proportional headcount growth",
            "Vendor management and contract sprawl",
        ],
        "kpis": ["process cycle time", "operational cost ratio", "employee productivity", "SLA compliance"],
        "triggers": ["organizational restructure", "process audit", "scaling challenges", "cost reduction mandate"],
        "messaging_angles": [
            "Lead with process automation and cycle time reduction",
            "Frame as operational visibility tool",
            "Show headcount-to-output ratio improvements",
            "Reference cross-department coordination benefits",
        ],
        "value_drivers": ["operational efficiency", "process visibility", "scalable operations"],
    },
    {
        "role": "SDR/BDR",
        "aliases": ["sdr", "bdr", "sales development representative", "business development representative"],
        "pain_points": [
            "Low response rates on outreach",
            "Spending too much time researching prospects",
            "Difficulty getting past gatekeepers",
            "Email deliverability issues",
            "Quota pressure with limited tools",
        ],
        "kpis": ["meetings booked", "response rate", "emails sent", "calls made", "conversion rate"],
        "triggers": ["new territory assignment", "tool evaluation", "low performance review"],
        "messaging_angles": [
            "Lead with response rate improvements",
            "Show time saved on research and personalization",
            "Frame as career advancement enabler (hit quota, get promoted)",
            "Reference peer success stories at similar companies",
        ],
        "value_drivers": ["response rates", "time savings", "quota achievement"],
    },
    {
        "role": "RevOps",
        "aliases": ["revenue operations", "revops manager", "head of revops", "director of revenue operations"],
        "pain_points": [
            "Data silos between sales, marketing, and CS systems",
            "Forecasting inaccuracy due to dirty pipeline data",
            "Manual reporting consuming analyst bandwidth",
            "Tech stack sprawl and integration maintenance",
            "Process inconsistency across go-to-market teams",
        ],
        "kpis": ["forecast accuracy", "data quality score", "tech stack ROI", "process adoption rate"],
        "triggers": ["CRM migration", "data quality initiative", "GTM alignment project", "new leadership"],
        "messaging_angles": [
            "Lead with data unification and single source of truth",
            "Frame as forecast accuracy enabler",
            "Show integration simplicity and maintenance reduction",
            "Reference GTM alignment success stories",
        ],
        "value_drivers": ["data quality", "forecast accuracy", "GTM alignment"],
    },
    {
        "role": "CHRO",
        "aliases": ["chief human resources officer", "vp hr", "head of hr", "vp people", "head of people", "people director"],
        "pain_points": [
            "Talent acquisition in competitive markets",
            "Employee retention and engagement decline",
            "DEI initiative measurement and accountability",
            "Compliance across multiple jurisdictions",
            "Remote/hybrid work policy management",
        ],
        "kpis": ["time to hire", "retention rate", "employee NPS", "DEI metrics", "compliance score"],
        "triggers": ["rapid hiring phase", "high attrition period", "policy overhaul", "new market entry"],
        "messaging_angles": [
            "Lead with hiring velocity and quality metrics",
            "Frame as retention and engagement tool",
            "Show compliance automation benefits",
            "Reference culture-building at scale",
        ],
        "value_drivers": ["talent acquisition speed", "employee retention", "compliance automation"],
    },
    {
        "role": "Product Manager",
        "aliases": ["pm", "product manager", "senior product manager", "head of product", "vp product", "director of product"],
        "pain_points": [
            "Prioritization across competing stakeholder demands",
            "Limited customer insight for roadmap decisions",
            "Feature bloat vs. core product focus",
            "Cross-team coordination for launches",
            "Measuring feature impact post-release",
        ],
        "kpis": ["feature adoption", "NPS", "time to value", "roadmap delivery rate", "customer feedback score"],
        "triggers": ["product launch", "customer churn spike", "competitive feature gap", "strategic pivot"],
        "messaging_angles": [
            "Lead with customer insight and feedback loop benefits",
            "Frame as prioritization clarity tool",
            "Show feature impact measurement capabilities",
            "Reference successful launches at similar companies",
        ],
        "value_drivers": ["customer insight", "roadmap clarity", "feature impact measurement"],
    },
    {
        "role": "IT Director",
        "aliases": ["it director", "it manager", "head of it", "vp it", "director of information technology"],
        "pain_points": [
            "Shadow IT and ungoverned SaaS sprawl",
            "Security incidents and vulnerability management",
            "Help desk ticket volume and resolution time",
            "Budget constraints vs. modernization demands",
            "Vendor lock-in and migration complexity",
        ],
        "kpis": ["uptime", "ticket resolution time", "security incidents", "SaaS spend", "compliance score"],
        "triggers": ["security incident", "budget review", "audit finding", "digital transformation initiative"],
        "messaging_angles": [
            "Lead with security and governance benefits",
            "Frame as SaaS spend optimization",
            "Show simplified vendor management",
            "Reference migration and integration ease",
        ],
        "value_drivers": ["security posture", "cost optimization", "governance"],
    },
    {
        "role": "Customer Success",
        "aliases": ["cs manager", "customer success manager", "head of customer success", "vp customer success", "director of customer success"],
        "pain_points": [
            "Churn signals detected too late to intervene",
            "Onboarding bottleneck delaying time to value",
            "Manual health scoring unreliable at scale",
            "Expansion revenue targets with limited resources",
            "Cross-functional coordination for at-risk accounts",
        ],
        "kpis": ["NRR", "churn rate", "time to value", "CSAT", "expansion revenue"],
        "triggers": ["churn spike", "renewal season", "team scaling", "CS platform evaluation"],
        "messaging_angles": [
            "Lead with early churn detection capabilities",
            "Frame as time-to-value accelerator",
            "Show health scoring automation benefits",
            "Reference expansion revenue success stories",
        ],
        "value_drivers": ["churn prevention", "expansion revenue", "customer satisfaction"],
    },
    {
        "role": "Data/Analytics Lead",
        "aliases": ["data analyst", "data scientist", "head of analytics", "analytics manager", "bi manager", "data engineer"],
        "pain_points": [
            "Data pipeline reliability and freshness issues",
            "Ad-hoc report requests consuming team bandwidth",
            "Data quality and governance across sources",
            "Self-service analytics adoption lagging",
            "Scaling data infrastructure for growing data volumes",
        ],
        "kpis": ["data freshness", "query performance", "self-service adoption", "data quality score"],
        "triggers": ["data migration", "BI tool evaluation", "data quality initiative", "team expansion"],
        "messaging_angles": [
            "Lead with data pipeline reliability improvements",
            "Frame as self-service enabler that frees analyst time",
            "Show data quality governance capabilities",
            "Reference scalability for growing data volumes",
        ],
        "value_drivers": ["data reliability", "self-service analytics", "data governance"],
    },
    {
        "role": "Legal/Compliance",
        "aliases": ["general counsel", "head of legal", "compliance officer", "vp legal", "chief compliance officer"],
        "pain_points": [
            "Contract review bottleneck slowing deal velocity",
            "Regulatory change tracking across jurisdictions",
            "Data privacy compliance (GDPR, CCPA, etc.)",
            "Vendor risk assessment at scale",
            "IP protection and monitoring",
        ],
        "kpis": ["contract turnaround time", "compliance audit pass rate", "legal spend", "risk incidents"],
        "triggers": ["regulatory change", "audit finding", "data breach concern", "M&A due diligence"],
        "messaging_angles": [
            "Lead with contract acceleration and review automation",
            "Frame as compliance risk reduction",
            "Show regulatory change tracking capabilities",
            "Reference vendor risk management at scale",
        ],
        "value_drivers": ["risk reduction", "compliance automation", "deal velocity"],
    },
    {
        "role": "Supply Chain",
        "aliases": ["supply chain manager", "head of supply chain", "vp supply chain", "logistics manager", "procurement director"],
        "pain_points": [
            "Demand forecasting inaccuracy causing overstock or stockouts",
            "Supplier reliability and lead time variability",
            "Logistics cost optimization under margin pressure",
            "Lack of end-to-end supply chain visibility",
            "Sustainability and ESG reporting requirements",
        ],
        "kpis": ["order accuracy", "inventory turns", "lead time", "logistics cost per unit", "supplier score"],
        "triggers": ["supply disruption", "cost reduction mandate", "new market entry", "sustainability audit"],
        "messaging_angles": [
            "Lead with demand forecasting accuracy improvements",
            "Frame as supply chain visibility and resilience tool",
            "Show logistics cost optimization results",
            "Reference sustainability and ESG benefits",
        ],
        "value_drivers": ["supply chain visibility", "cost optimization", "demand accuracy"],
    },
    {
        "role": "Account Executive",
        "aliases": ["ae", "account executive", "senior account executive", "enterprise account executive"],
        "pain_points": [
            "Spending too much time on proposal and admin work",
            "Difficulty multi-threading into accounts",
            "Losing deals to competitors on differentiation",
            "Long sales cycles with multiple stakeholders",
            "Inconsistent access to competitive intelligence",
        ],
        "kpis": ["closed revenue", "win rate", "deal size", "sales cycle length", "pipeline coverage"],
        "triggers": ["deal loss to competitor", "new territory", "quota increase", "enablement gap"],
        "messaging_angles": [
            "Lead with deal acceleration and admin time reduction",
            "Frame as competitive intelligence advantage",
            "Show multi-threading and stakeholder mapping benefits",
            "Reference win rate improvements at similar orgs",
        ],
        "value_drivers": ["win rate improvement", "deal acceleration", "competitive advantage"],
    },
    {
        "role": "Marketing Manager",
        "aliases": ["marketing manager", "demand gen manager", "growth manager", "digital marketing manager"],
        "pain_points": [
            "Campaign performance plateauing despite budget increases",
            "Content creation bottleneck across channels",
            "Lead scoring models not reflecting actual buyer intent",
            "Attribution gaps across paid, organic, and outbound",
            "Difficulty personalizing at scale",
        ],
        "kpis": ["MQLs", "CPL", "conversion rate", "content output", "channel ROI"],
        "triggers": ["campaign underperformance", "new channel launch", "martech evaluation", "budget review"],
        "messaging_angles": [
            "Lead with campaign performance optimization results",
            "Frame as personalization at scale enabler",
            "Show attribution clarity across channels",
            "Reference content production acceleration",
        ],
        "value_drivers": ["campaign performance", "personalization scale", "attribution clarity"],
    },
    {
        "role": "Engineering Manager",
        "aliases": ["engineering manager", "dev manager", "team lead engineering", "staff engineer"],
        "pain_points": [
            "Sprint velocity declining with growing codebase",
            "Developer experience friction slowing onboarding",
            "Production incidents interrupting planned work",
            "Technical debt conversations with product team",
            "Hiring pipeline not keeping pace with attrition",
        ],
        "kpis": ["sprint velocity", "deployment frequency", "code review time", "incident rate", "team satisfaction"],
        "triggers": ["team scaling", "incident spike", "developer survey results", "tool consolidation"],
        "messaging_angles": [
            "Lead with developer productivity and experience gains",
            "Frame as incident reduction tool",
            "Show onboarding acceleration for new engineers",
            "Reference codebase quality improvements",
        ],
        "value_drivers": ["developer productivity", "code quality", "team satisfaction"],
    },
    {
        "role": "Consultant/Agency Owner",
        "aliases": ["consultant", "agency owner", "agency director", "managing consultant", "principal consultant"],
        "pain_points": [
            "Client acquisition cost eating into margins",
            "Scaling delivery without proportional headcount",
            "Scope creep eroding profitability",
            "Differentiating from larger competitors",
            "Pipeline unpredictability and feast-or-famine cycles",
        ],
        "kpis": ["utilization rate", "client retention", "project margin", "pipeline value", "referral rate"],
        "triggers": ["lost pitch", "capacity crunch", "new service offering", "partnership opportunity"],
        "messaging_angles": [
            "Lead with client acquisition efficiency gains",
            "Frame as delivery scaling without headcount",
            "Show differentiation and positioning benefits",
            "Reference pipeline stabilization results",
        ],
        "value_drivers": ["client acquisition", "delivery efficiency", "pipeline stability"],
    },
    {
        "role": "HR Manager",
        "aliases": ["hr manager", "talent acquisition manager", "recruiter", "people operations manager"],
        "pain_points": [
            "Time-to-hire too long for critical roles",
            "Candidate experience inconsistency across recruiters",
            "Offer acceptance rate declining",
            "Employer brand not resonating with target talent",
            "Onboarding process manual and fragmented",
        ],
        "kpis": ["time to hire", "offer acceptance rate", "candidate NPS", "cost per hire", "90-day retention"],
        "triggers": ["hiring surge", "attrition spike", "employer brand refresh", "ATS evaluation"],
        "messaging_angles": [
            "Lead with time-to-hire reduction results",
            "Frame as candidate experience differentiator",
            "Show onboarding automation benefits",
            "Reference employer brand improvement stories",
        ],
        "value_drivers": ["hiring speed", "candidate experience", "onboarding efficiency"],
    },
    {
        "role": "Ecommerce Manager",
        "aliases": ["ecommerce manager", "head of ecommerce", "digital commerce director", "online sales manager"],
        "pain_points": [
            "Conversion rate optimization hitting diminishing returns",
            "Cart abandonment rate stubbornly high",
            "Personalization limited by data fragmentation",
            "Inventory sync issues across channels",
            "Rising customer acquisition costs",
        ],
        "kpis": ["conversion rate", "AOV", "cart abandonment rate", "customer lifetime value", "ROAS"],
        "triggers": ["peak season prep", "platform migration", "channel expansion", "CAC crisis"],
        "messaging_angles": [
            "Lead with conversion rate and AOV improvement data",
            "Frame as personalization at scale enabler",
            "Show cross-channel inventory sync benefits",
            "Reference customer lifetime value growth stories",
        ],
        "value_drivers": ["conversion optimization", "personalization", "customer lifetime value"],
    },
]

# ──────────────────────────────────────────────
# Lookup Functions
# ──────────────────────────────────────────────

def find_matching_persona(title: str) -> dict | None:
    """Find the best matching persona card for a prospect's title.

    Args:
        title: The prospect's job title (e.g., "VP of Sales", "CTO")

    Returns:
        Matching persona dict or None if no match found.
    """
    if not title:
        return None

    title_lower = title.lower().strip()

    # Direct role match
    for persona in PERSONA_CARDS:
        if contains_term(title_lower, persona["role"]):
            return persona

    # Alias match
    for persona in PERSONA_CARDS:
        for alias in persona["aliases"]:
            if contains_term(title_lower, alias):
                return persona

    # Partial keyword match (less strict)
    title_words = set(re.sub(r"[^a-z0-9 ]", "", title_lower).split())
    best_match = None
    best_score = 0

    for persona in PERSONA_CARDS:
        all_terms = [persona["role"].lower()] + persona["aliases"]
        for term in all_terms:
            term_words = set(re.sub(r"[^a-z0-9 ]", "", term).split())
            if not term_words:
                continue
            overlap = len(title_words & term_words)
            score = overlap / len(term_words)
            if score > best_score and score >= 0.5:
                best_score = score
                best_match = persona

    return best_match


def get_all_personas_summary() -> str:
    """Get a concise summary of all persona cards for prompt injection."""
    lines = []
    for p in PERSONA_CARDS:
        pains = "; ".join(p["pain_points"][:3])
        lines.append(f"- **{p['role']}**: {pains}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Archive persona alias map (9 Sep 2026, KB v2 14 Sep 2026)
# ──────────────────────────────────────────────
#
# The shipped sales-methodology corpus (`heylead/data/kb/persona_cards.jsonl`)
# keys every card by its own persona vocabulary — `COO_HEAD_OPS`,
# `FOUNDER_CEO`, `CCO_COMPLIANCE`, … — which is not the vocabulary of the 22
# hand-written cards above. Without this map a title lookup finds a HeyLead
# persona and then retrieves nothing. ("Archive" is the v1 name, from the
# AI in Charge archive the first corpus came from; the names stay so callers
# keep working.)
#
# KB v2 has 17 personas. It retired SDR, CDO_DIVERSITY,
# CHIEF_RESEARCH_OFFICER, PRESIDENT, CINO and CIO_INVESTMENT (their titles now
# reach a covering persona, or none) and added HEAD_OF_PRODUCT, AGENCY_OWNER
# and HEAD_OF_ECOMMERCE. Five HeyLead personas have no counterpart (Account
# Executive, Marketing Manager, HR Manager, SDR/BDR, Data/Analytics Lead).
# They stay hand-written and simply retrieve no book evidence — that is the
# intended behaviour, not a gap to paper over.

ARCHIVE_PERSONA_ALIASES: dict[str, str] = {
    "CFO": "CFO",
    "CRO": "CRO",
    "CMO": "CMO",
    "CTO": "CTO",
    "CHRO": "CHRO",
    "CIO": "IT Director",
    "CSO_SECURITY": "IT Director",
    "CSO_STRATEGY": "CEO/Founder",
    "CCO_COMPLIANCE": "Legal/Compliance",
    "CCO_CUSTOMER": "Customer Success",
    "COO_HEAD_OPS": "COO/Head of Ops",
    "FOUNDER_CEO": "CEO/Founder",
    "REVOPS": "RevOps",
    "VP_SALES": "VP Sales",
    "HEAD_OF_PRODUCT": "Product Manager",
    "AGENCY_OWNER": "Consultant/Agency Owner",
    "HEAD_OF_ECOMMERCE": "Ecommerce Manager",
}

# HeyLead persona role -> persona keys that carry its book evidence.
# Several HeyLead roles borrow a neighbouring persona because the books treat
# them as the same buyer (Engineering Manager reads as CTO, Supply Chain as
# head of operations). An empty list is a role with no book evidence.
HEYLEAD_TO_ARCHIVE_PERSONAS: dict[str, list[str]] = {
    "CFO": ["CFO"],
    "CRO": ["CRO"],
    "CMO": ["CMO"],
    "CTO": ["CTO"],
    "CEO/Founder": ["FOUNDER_CEO", "CSO_STRATEGY"],
    "VP Sales": ["VP_SALES"],
    "COO/Head of Ops": ["COO_HEAD_OPS"],
    "SDR/BDR": [],
    "RevOps": ["REVOPS"],
    "CHRO": ["CHRO"],
    "IT Director": ["CIO", "CSO_SECURITY"],
    "Customer Success": ["CCO_CUSTOMER"],
    "Data/Analytics Lead": [],
    "Legal/Compliance": ["CCO_COMPLIANCE"],
    "Supply Chain": ["COO_HEAD_OPS"],
    "Engineering Manager": ["CTO"],
    "Product Manager": ["HEAD_OF_PRODUCT"],
    "Consultant/Agency Owner": ["AGENCY_OWNER"],
    "Ecommerce Manager": ["HEAD_OF_ECOMMERCE"],
}


# Direct title -> persona aliases. Byte-for-byte the same table as
# heylead-api `app/services/rag/kb.py:PERSONA_TITLE_ALIASES` (pinned by
# `tests/test_kb_retrieval_finds_persona_cards.py`) — "two definitions of one
# thing" is the trap that produced three different `sync_connections`.
# Change one, change both.
#
# It exists because the 22 hand-written cards cover a different set of roles:
# a CISO or a chief investment officer has book evidence and no HeyLead
# persona card to route through. Whole-phrase matching only; first alias wins
# in dict order, so the narrower AGENCY_OWNER sits before FOUNDER_CEO ("agency
# owner" also contains "owner").
ARCHIVE_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "CFO": ("cfo", "chief financial officer", "finance director", "vp finance",
            "head of finance", "financial controller",
            # was CIO_INVESTMENT: an investment chief buys the way finance does
            "chief investment officer", "portfolio manager", "head of investments"),
    "CRO": ("cro", "chief revenue officer", "vp revenue", "head of revenue",
            "chief commercial officer", "commercial director", "head of business development",
            "business development director"),
    "CMO": ("cmo", "chief marketing officer", "vp marketing", "head of marketing",
            "marketing director", "director of marketing", "head of growth", "vp growth",
            "chief growth officer"),
    "CTO": ("cto", "chief technology officer", "vp engineering", "head of engineering",
            "vp technology", "engineering director", "engineering manager",
            "chief architecture officer", "chief architect", "head of architecture",
            "chief technical officer", "director of engineering", "vp software engineering",
            "head of ai", "chief data officer", "head of data", "technical director",
            "technology director", "director of technology", "president of engineering",
            "president of technology"),
    "CIO": ("cio", "chief information officer", "it director", "head of it",
            "vp information technology"),
    "CSO_SECURITY": ("ciso", "chief security officer", "chief information security officer",
                     "head of security", "vp security"),
    "CSO_STRATEGY": ("chief strategy officer", "head of strategy", "vp strategy",
                     # was CINO
                     "chief innovation officer", "head of innovation",
                     "chief transformation officer", "head of transformation"),
    "CCO_COMPLIANCE": ("chief compliance officer", "head of compliance", "general counsel",
                       "head of legal", "vp legal", "chief risk officer", "head of risk",
                       "operational risk", "risk director"),
    "CCO_CUSTOMER": ("chief customer officer", "vp customer success",
                     "head of customer success", "customer success director"),
    "CHRO": ("chro", "chief people officer", "chief human resources officer",
             "vp people", "head of people", "vp human resources", "hr director",
             # was CDO_DIVERSITY
             "chief diversity officer", "head of diversity", "head of dei", "vp diversity",
             "people operations",
             "head of hr", "human resources director", "people director", "head of talent",
             "head of talent acquisition", "head of recruitment"),
    "COO_HEAD_OPS": ("coo", "chief operating officer", "vp operations",
                     "head of operations", "operations director", "head of supply chain"),
    "AGENCY_OWNER": ("agency owner", "agency founder", "agency director", "managing partner",
                     "consultancy owner", "consultancy founder", "principal consultant"),
    "FOUNDER_CEO": ("ceo", "founder", "co-founder", "cofounder",
                    "chief executive officer", "managing director", "owner",
                    # was PRESIDENT
                    "president", "general manager"),
    "HEAD_OF_PRODUCT": ("cpo", "chief product officer", "vp product", "head of product",
                        "product director", "director of product", "president of product"),
    "HEAD_OF_ECOMMERCE": ("head of ecommerce", "head of e-commerce", "ecommerce director",
                          "e-commerce director", "vp ecommerce", "vp e-commerce",
                          "director of ecommerce", "director of e-commerce",
                          "head of digital commerce"),
    "REVOPS": ("revops", "revenue operations", "sales operations", "head of revops",
               "sales ops", "gtm operations", "head of sales operations", "director of sales operations",
               "sales operations director", "head of revenue operations"),
    "VP_SALES": ("vp sales", "vp of sales", "head of sales", "sales director",
                 "director of sales", "chief sales officer", "president of sales"),
}

_ALIAS_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")


def _alias_stem(word: str) -> str:
    """Crude plural strip so "CFOs"/"CISOs" reach the CFO / CISO aliases."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


#: "and" and "for" stay in the text: they end a role phrase ("head of product
#: and engineering" is a head of product), and no alias contains them.
_TITLE_STOPWORDS = frozenset(("of", "the"))
#: Mirrors heylead-api kb.py. Spelled-out ranks rewritten before any alias is
#: read: "vice president of engineering" must never reach the alias "president".
_TITLE_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("senior vice president", "vp"), ("executive vice president", "vp"),
    ("vice president", "vp"), ("svp", "vp"), ("evp", "vp"),
)
_TITLE_RANK_PREFIXES = frozenset(("senior", "sr", "executive", "group", "global",
                                   "regional", "deputy", "interim", "acting", "fractional"))
#: An alias followed by one of these is the start of a longer noun phrase,
#: not the role: "head of product security" is security, "head of product
#: design" is design. A longer alias that covers the whole phrase still wins.
_FUNCTION_CHANGERS = frozenset(("security", "design", "marketing", "analytics", "data", "research",
                                "quality", "compliance", "risk", "operations", "ops",
                                "science", "content", "brand", "growth", "sales", "finance", "legal",
                                "hr", "people", "talent", "enablement", "education", "experience",
                                "ux", "ui", "safety", "partnerships", "success"))
_RANK_ONLY_WORDS = frozenset(("vp", "director", "head", "manager", "lead", "president"))
_PAST_ROLE_MARKERS = frozenset(("former", "formerly", "ex", "previously", "past", "retired"))
_NOT_THE_BUYER_PHRASES = ("founders office", "office of the ceo", "office of the cto",
                          "chief of staff", "office of the cio", "cio office", "cto office", "ceo office", "assistant", "intern", "student", "aspiring",
                          "seeking", "open to work", "candidate", "advisor to")
_SEGMENT_RE = re.compile(r"(?:\s*[|•·;,/()]+\s*|\s+[–—-]\s+|\s+@\s+|\s+at\s+)")


def _alias_normalize(text: str) -> str:
    # Mirrors heylead-api kb._alias_normalize: of/the/and/for defeat the
    # whole-phrase match ("vp of engineering" never contained " vp engineering ").
    low = (text or "").lower().replace("&", " and ")
    for long, short in _TITLE_SYNONYMS:
        low = re.sub(rf"\b{re.escape(long)}\b", short, low)
    words = [w for w in _ALIAS_SPLIT_RE.split(low) if w and w not in _TITLE_STOPWORDS]
    return " " + " ".join(_alias_stem(w) for w in words) + " "


@lru_cache(maxsize=1)
def _alias_index() -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (persona, _alias_normalize(alias), order)
        for order, (persona, aliases) in enumerate(ARCHIVE_TITLE_ALIASES.items())
        for alias in aliases
    )


def _title_segments(title: str) -> list[str]:
    pieces: list[list[str]] = []
    for raw in _SEGMENT_RE.split((title or "").lower()):
        words = _alias_normalize(raw).split()
        while words and words[0] in _TITLE_RANK_PREFIXES:
            words = words[1:]
        if words:
            pieces.append(words)
    out: list[str] = []
    i = 0
    while i < len(pieces):
        words = pieces[i]
        # "Director, Product Management" and "VP, Engineering" write the
        # function after a comma: a piece that is only a rank joins the next.
        if i + 1 < len(pieces) and all(w in _RANK_ONLY_WORDS for w in words):
            words = words + pieces[i + 1]
            i += 1
        i += 1
        if words[0] in _PAST_ROLE_MARKERS:
            continue
        seg = " " + " ".join(words) + " "
        if any(_alias_normalize(p) in seg for p in _NOT_THE_BUYER_PHRASES):
            continue
        out.append(seg)
    return out


def _match_segment(segment: str) -> str | None:
    best: tuple[int, int, int] | None = None
    found: str | None = None
    for persona, alias, order in _alias_index():
        pos = segment.find(alias)
        if pos < 0:
            continue
        following = segment[pos + len(alias):].split()
        if following and following[0] in _FUNCTION_CHANGERS:
            continue
        key = (pos, -len(alias), order)
        if best is None or key < best:
            best, found = key, persona
    return found


def archive_personas_for_title(title: str) -> list[str]:
    """Archive persona keys whose book cards apply to this job title.

    Whole-phrase alias match only — never a substring. "director" matching
    inside "successfactors" is the bug family this house rule exists for
    (22 Aug 2026: every Director was a perfect CTO match). A title with no
    alias returns [], which is the honest answer: Account Executive,
    Marketing Manager, HR Manager, SDR/BDR and data/research roles have no
    persona in the KB and must not borrow a neighbour's. The v2 product,
    agency and ecommerce personas are reached by their buying titles (Head
    of Product, Agency Owner, Head of E-commerce), not by an individual
    contributor title such as "Product Manager". The title is usually the
    headline, read segment by segment in the order written; the alias written
    earliest in the first segment that names a buyer decides -- the same rules
    as heylead-api `personas_for_titles`.
    """
    for segment in _title_segments(title):
        persona = _match_segment(segment)
        if persona:
            return [persona]
    return []
