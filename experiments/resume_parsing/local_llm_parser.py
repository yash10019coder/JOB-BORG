#!/usr/bin/env python3
"""
Local LLM resume parser using Ollama
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
import pdfplumber
import subprocess


# Same schema as cloud parser
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


class LocalLLMParser:
    def __init__(self, model: str = "gemma4:e2b"):
        self.model = model
        # Check if model exists
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True)
        if model not in result.stdout:
            print(f"Warning: Model {model} not found. Available: {result.stdout}")
    
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
        """Parse resume using local LLM"""
        text = self.extract_text(pdf_path)
        
        # Truncate for context window
        if len(text) > 12000:
            text = text[:12000] + "\n[TRUNCATED]"
        
        prompt = f"{RESUME_SCHEMA}\n\nResume text:\n{text}"
        
        try:
            result = subprocess.run(
                ['ollama', 'run', self.model, prompt],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                return {"error": result.stderr, "raw_text": text[:500]}
            
            output = result.stdout.strip()
            
            # Extract JSON from output (LLM might add markdown)
            json_match = re.search(r'\{.*\}', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {"error": "No JSON found in output", "raw_output": output[:500]}
                
        except subprocess.TimeoutExpired:
            return {"error": "Timeout", "raw_text": text[:500]}
        except Exception as e:
            return {"error": str(e), "raw_text": text[:500]}


def main():
    parser = LocalLLMParser(model="gemma4:e2b")
    
    test_dir = Path('/home/ubuntu/corporate/jobhubt/jobborg/experiments/resume_parsing/test_resumes')
    
    for pdf_file in sorted(test_dir.glob('resume_*.pdf')):
        print(f"\n{'='*60}")
        print(f"Parsing: {pdf_file.name}")
        print(f"{'='*60}")
        
        result = parser.parse(str(pdf_file))
        print(json.dumps(result, indent=2, default=str))
        
        # Save for comparison
        out_file = test_dir / f"{pdf_file.stem}_local_llm.json"
        out_file.write_text(json.dumps(result, indent=2))
        print(f"Saved to {out_file}")


if __name__ == '__main__':
    main()