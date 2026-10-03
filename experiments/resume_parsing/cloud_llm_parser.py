#!/usr/bin/env python3
"""
Cloud LLM resume parser using OpenRouter API
"""

import os
import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
import pdfplumber
from openai import OpenAI


# Job-agent resume_inventory.json schema prompt
RESUME_SCHEMA = """
Extract the resume into this exact JSON schema:

{
  "contact": {
    "name": "string",
    "email": "string",
    "phone": "string",
    "linkedin": "string",
    "github": "string",
    "location": "string"
  },
  "lane_profiles": {
    "key": {
      "lane": "string",
      "title": "string",
      "headline": "string",
      "summary": "string",
      "role_order": ["role_id1", "role_id2"],
      "preferred_projects": ["project_id1", "project_id2"],
      "skills_priority": ["category1", "category2"],
      "preferred_achievements": ["ach_id1"]
    }
  },
  "education": {
    "school": "string",
    "degree": "string",
    "location": "string",
    "start": "string",
    "end": "string",
    "cgpa": "string"
  },
  "skills": [
    {"name": "Category", "items": ["skill1", "skill2"], "priority": "high|medium|low"}
  ],
  "roles": [
    {
      "id": "unique_id",
      "company": "string",
      "title": "string",
      "location": "string",
      "start": "string",
      "end": "string",
      "tech_stack": ["tech1", "tech2"],
      "bullets": [
        {
          "id": "unique_bullet_id",
          "text": "bullet text with metrics",
          "lane_tags": ["lane1"],
          "skills": ["skill1", "skill2"],
          "metrics": ["metric1", "metric2"],
          "evidence_strength": "high|medium|low",
          "project_or_role_source": "Company Name"
        }
      ]
    }
  ],
  "projects": [
    {
      "id": "unique_id",
      "name": "string",
      "url": "string",
      "tech_stack": ["tech1", "tech2"],
      "bullets": [
        {
          "id": "unique_bullet_id",
          "text": "bullet text",
          "lane_tags": ["lane1"],
          "skills": ["skill1"],
          "metrics": [],
          "evidence_strength": "high|medium|low",
          "project_or_role_source": "Project Name"
        }
      ]
    }
  ],
  "achievements": [
    {"id": "unique_id", "title": "string", "detail": "string", "lane_tags": [], "link": null}
  ]
}

Rules:
1. Infer lane from experience (backend, frontend, ml, devops, fullstack, mobile, etc.)
2. Extract YEARS of experience per skill from bullets (e.g., "3 years Java" → skill "Java" with 3 years)
3. Extract metrics from bullets (numbers, percentages, scale indicators)
4. Create unique IDs for roles, projects, bullets, achievements
5. If info missing, use null or empty array
6. Return ONLY valid JSON, no markdown, no explanation
"""


class CloudLLMParser:
    def __init__(self, api_key: str = None, model: str = "openai/gpt-4o-mini"):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY required")
        
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key
        )
        self.model = model
    
    def extract_text(self, pdf_path: str) -> str:
        """Extract text from PDF"""
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return '\n'.join(text_parts)
    
    def parse(self, pdf_path: str) -> Dict[str, Any]:
        """Parse resume using LLM"""
        text = self.extract_text(pdf_path)
        
        # Truncate if too long (keep first 15000 chars for cost)
        if len(text) > 15000:
            text = text[:15000] + "\n[TRUNCATED]"
        
        prompt = f"{RESUME_SCHEMA}\n\nResume text:\n{text}"
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a precise resume parser. Output only valid JSON matching the schema."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=4000,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            return json.loads(content)
            
        except Exception as e:
            print(f"LLM parsing error: {e}")
            return {"error": str(e), "raw_text": text[:500]}


def main():
    # Check for API key
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ERROR: OPENROUTER_API_KEY not set")
        print("Set it: export OPENROUTER_API_KEY='your-key'")
        return
    
    parser = CloudLLMParser()
    
    test_dir = Path('/home/ubuntu/corporate/jobhubt/jobborg/experiments/resume_parsing/test_resumes')
    
    for pdf_file in sorted(test_dir.glob('resume_*.pdf')):
        print(f"\n{'='*60}")
        print(f"Parsing: {pdf_file.name}")
        print(f"{'='*60}")
        
        result = parser.parse(str(pdf_file))
        print(json.dumps(result, indent=2, default=str))
        
        # Save for comparison
        out_file = test_dir / f"{pdf_file.stem}_llm.json"
        out_file.write_text(json.dumps(result, indent=2))
        print(f"Saved to {out_file}")


if __name__ == '__main__':
    main()