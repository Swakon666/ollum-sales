from __future__ import annotations

import os
from typing import Any

from scrapegraphai.graphs import SmartScraperGraph

from .config import settings
from .schemas import LeadAnalysis


LEAD_PROMPT = """
Analyze this website as a potential B2B lead for Ollum Group, a software studio that
builds business websites, web apps, Telegram/WhatsApp/MAX bots, automation and AI integrations.

Return only structured information matching the requested schema.

Evaluate:
- company name, industry, location and a short factual summary;
- public contact details present on the website;
- strengths and concrete weaknesses of the current website;
- detected digital tools/integrations if visible;
- realistic opportunities where Ollum Group could create business value;
- which Ollum services fit the company;
- 2-4 personalized outreach angles grounded in the actual website;
- lead_score from 0 to 100, where a high score means strong fit and visible need;
- a short explanation of the score.

Do not invent facts that are not present or reasonably inferable from the site. If data is missing,
leave the corresponding field empty.
""".strip()


def _llm_config() -> dict[str, Any]:
    model = settings.scrapegraph_model
    llm: dict[str, Any] = {"model": model}

    # ScrapeGraphAI accepts api_key inside the llm config for hosted providers.
    key = settings.llm_api_key or settings.openai_api_key
    if key:
        llm["api_key"] = key

    return llm


def analyze_website(url: str, extra_context: str | None = None) -> dict[str, Any]:
    prompt = LEAD_PROMPT
    if extra_context:
        prompt += f"\n\nAdditional sales context from the operator:\n{extra_context.strip()}"

    graph = SmartScraperGraph(
        prompt=prompt,
        source=url,
        config={
            "llm": _llm_config(),
            "verbose": False,
            "headless": True,
            "reasoning": True,
            "reattempt": True,
        },
        schema=LeadAnalysis,
    )
    result = graph.run()

    if isinstance(result, LeadAnalysis):
        return result.model_dump()
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {"raw": result}
