from __future__ import annotations

from pydantic import BaseModel, Field


class ContactInfo(BaseModel):
    phones: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    messengers: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)


class LeadAnalysis(BaseModel):
    company_name: str | None = None
    industry: str | None = None
    location: str | None = None
    summary: str | None = None
    contacts: ContactInfo = Field(default_factory=ContactInfo)
    website_strengths: list[str] = Field(default_factory=list)
    website_problems: list[str] = Field(default_factory=list)
    detected_tools: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    recommended_ollum_services: list[str] = Field(default_factory=list)
    outreach_angles: list[str] = Field(default_factory=list)
    lead_score: int = Field(ge=0, le=100)
    score_reason: str | None = None
