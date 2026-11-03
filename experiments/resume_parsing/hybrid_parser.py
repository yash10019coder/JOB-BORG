#!/usr/bin/env python3
"""
Hybrid resume parser: Rule-based (primary) + Local LLM (enhancement for fuzzy fields)
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
import pdfplumber
import subprocess


class HybridParser:
    def __init__(self, llm_model: str = "gemma2:2b"):
        self.llm_model = llm_model
        self.rule_parser = RuleBasedParser()
        
        # Check if LLM model exists
        result = subprocess.run(['ollama', 'list'], capture_output=True, text=True)
        self.llm_available = llm_model in result.stdout
    
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
        """Parse resume using hybrid approach"""
        # Step 1: Rule-based extraction (always runs)
        rule_result = self.rule_parser.parse(pdf_path)
        
        # Step 2: LLM enhancement for fuzzy fields (if available)
        if self.llm_available:
            llm_enhancement = self._llm_enhance(pdf_path)
            if llm_enhancement and "error" not in llm_enhancement:
                rule_result = self._merge_results(rule_result, llm_enhancement)
        
        return rule_result
    
    def _llm_enhance(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        """Use LLM to extract fuzzy fields: years per skill, metrics, project descriptions"""
        text = self.extract_text(pdf_path)
        if len(text) > 8000:
            text = text[:8000] + "\n[TRUNCATED]"
        
        prompt = f"""Extract ONLY these fields from the resume as JSON:
{{
  "skills_with_years": {{"skill": "years"}},
  "role_metrics": ["metric1", "metric2"],
  "project_descriptions": [{{"name": "...", "description": "...", "tech": ["..."]}}]
}}

Resume:
{text}

Return ONLY valid JSON."""
        
        try:
            result = subprocess.run(
                ['ollama', 'run', self.llm_model, prompt],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode != 0:
                return None
            
            output = result.stdout.strip()
            json_match = re.search(r'\{.*\}', output, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception:
            pass
        return None
    
    def _merge_results(self, rule_result: Dict, llm_result: Dict) -> Dict:
        """Merge LLM enhancement into rule-based result"""
        # Add years to skills
        if "skills_with_years" in llm_result:
            for skill, years in llm_result["skills_with_years"].items():
                # Find matching skill in rule result
                for i, s in enumerate(rule_result.get("skills", [])):
                    if s.lower() == skill.lower():
                        rule_result["skills"][i] = f"{s} ({years})"
                        break
        
        # Add metrics to experience
        if "role_metrics" in llm_result:
            for exp in rule_result.get("experience", []):
                exp["llm_metrics"] = llm_result["role_metrics"]
        
        # Add project descriptions
        if "project_descriptions" in llm_result:
            rule_result["llm_projects"] = llm_result["project_descriptions"]
        
        return rule_result


class RuleBasedParser:
    """Rule-based parser from earlier"""
    def __init__(self):
        self.known_skills = {
            'java', 'go', 'golang', 'kotlin', 'python', 'javascript', 'typescript',
            'sql', 'bash', 'c++', 'c', 'r', 'rust', 'scala', 'ruby', 'php',
            'spring boot', 'spring', 'gin', 'express', 'express.js', 'fastapi',
            'django', 'flask', 'react', 'next.js', 'nextjs', 'vue', 'angular',
            'redux', 'tailwind', 'styled-components', 'webpack', 'vite',
            'docker', 'kubernetes', 'k8s', 'github actions', 'ci/cd', 'jenkins',
            'terraform', 'terragrunt', 'pulumi', 'crossplane', 'helm', 'kustomize',
            'argo', 'argocd', 'flux', 'istio', 'linkerd', 'cert-manager',
            'aws', 'gcp', 'azure', 'eks', 'gke', 'aks', 'ec2', 'rds', 's3',
            'lambda', 'cloudfront', 'iam', 'cloud run', 'cloud sql', 'bigquery',
            'vertex ai', 'sagemaker', 'emr',
            'postgresql', 'postgres', 'mysql', 'mongodb', 'redis', 'clickhouse',
            'firebase', 'dynamodb', 'cassandra', 'elasticsearch',
            'pytorch', 'tensorflow', 'hugging face', 'transformers', 'bert', 'gpt',
            'scikit-learn', 'sklearn', 'xgboost', 'lightgbm', 'spacy', 'nltk',
            'sentence-transformers', 'deepspeed', 'lora', 'qlora',
            'spark', 'kafka', 'flink', 'airflow', 'mlflow', 'kubeflow',
            'delta lake', 'feast',
            'prometheus', 'grafana', 'loki', 'tempo', 'jaeger', 'alertmanager',
            'kubecost',
            'junit', 'mockito', 'jest', 'react testing library', 'cypress', 'playwright',
            'pytest', 'espresso', 'uiautomator',
            'git', 'figma', 'storybook', 'eslint', 'prettier', 'vault', 'opa',
            'gatekeeper', 'falco', 'trivy', 'cosign', 'kyverno', 'backstage',
        }
        
    def extract_text(self, pdf_path: str) -> str:
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
        return '\n'.join(text_parts)
    
    def extract_contact(self, text: str) -> Dict[str, str]:
        contact = {}
        email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
        if email_match:
            contact['email'] = email_match.group()
        phone_match = re.search(r'[\+]?[\d\s\-\(\)]{10,}', text)
        if phone_match:
            contact['phone'] = phone_match.group().strip()
        linkedin_match = re.search(r'linkedin\.com/in/[\w\-]+', text, re.IGNORECASE)
        if linkedin_match:
            contact['linkedin'] = 'https://' + linkedin_match.group()
        github_match = re.search(r'github\.com/[\w\-]+', text, re.IGNORECASE)
        if github_match:
            contact['github'] = 'https://' + github_match.group()
        location_patterns = [
            r'[Ll]ocation[:\s]+([^\n]+)',
            r'[Bb]ased in ([^\n]+)',
            r'[Rr]emote[^\n]*\(([^)]+)\)',
        ]
        for pattern in location_patterns:
            match = re.search(pattern, text)
            if match:
                contact['location'] = match.group(1).strip()
                break
        return contact
    
    def extract_name(self, text: str) -> Optional[str]:
        lines = text.strip().split('\n')
        for line in lines[:5]:
            line = line.strip()
            if not line or '@' in line or 'http' in line.lower():
                continue
            words = line.split()
            if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w):
                if not any(kw in line.lower() for kw in ['summary', 'skills', 'experience', 'education', 'projects', 'achievements', 'certifications', 'location']):
                    return line
        return None
    
    def extract_summary(self, text: str) -> Optional[str]:
        patterns = [
            r'[Ss]ummary[:\s]+\n(.*?)(?:\n\n|\n[A-Z]{2,}|\nEXPERIENCE|\nSKILLS|\nEDUCATION)',
            r'[Pp]rofile[:\s]+\n(.*?)(?:\n\n|\n[A-Z]{2,})',
            r'[Oo]bjective[:\s]+\n(.*?)(?:\n\n|\n[A-Z]{2,})',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if match:
                summary = match.group(1).strip()
                if len(summary) > 50:
                    return summary[:500]
        return None
    
    def extract_skills(self, text: str) -> List[str]:
        found_skills = set()
        text_lower = text.lower()
        for skill in self.known_skills:
            pattern = r'\b' + re.escape(skill) + r'\b'
            if re.search(pattern, text_lower):
                found_skills.add(skill.title())
        return sorted(found_skills)
    
    def extract_experience(self, text: str) -> List[Dict[str, Any]]:
        experiences = []
        exp_section = self._find_section(text, ['experience', 'employment', 'work history'])
        if not exp_section:
            return experiences
        
        company_pattern = r'([A-Z][\w\s&.,\-]+)\s*[—\-–]\s*([A-Z][\w\s]+)\s*\(([^)]+)\)\s*([^\n]*)'
        
        for match in re.finditer(company_pattern, exp_section):
            company = match.group(1).strip()
            role = match.group(2).strip()
            dates = match.group(3).strip()
            location = match.group(4).strip() if match.group(4) else ""
            
            start = match.end()
            next_match = re.search(company_pattern, exp_section[start:])
            end = start + next_match.start() if next_match else len(exp_section)
            bullets_text = exp_section[start:end]
            
            bullets = []
            for line in bullets_text.split('\n'):
                line = line.strip()
                if line.startswith(('•', '-', '*', '·')) or re.match(r'^\d+\.', line):
                    bullets.append(re.sub(r'^[•\-\*\·\d\.]\s*', '', line))
            
            experiences.append({
                'company': company,
                'role': role,
                'dates': dates,
                'location': location,
                'bullets': bullets[:10]
            })
        
        return experiences
    
    def extract_education(self, text: str) -> List[Dict[str, Any]]:
        education = []
        edu_section = self._find_section(text, ['education', 'academic'])
        if not edu_section:
            return education
        
        lines = edu_section.split('\n')
        current = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if any(kw in line.lower() for kw in ['bachelor', 'master', 'phd', 'b.tech', 'm.tech', 'b.s.', 'm.s.', 'b.sc', 'm.sc']):
                if current:
                    education.append(current)
                current = {'degree': line}
            elif 'university' in line.lower() or 'institute' in line.lower() or 'college' in line.lower():
                current['school'] = line
            elif re.search(r'\d{4}\s*[-–]\s*\d{4}', line):
                current['dates'] = line
            elif re.search(r'(india|usa|uk|canada|germany|france|sweden|mexico|remote)', line.lower()):
                current['location'] = line
        if current:
            education.append(current)
        
        return education
    
    def extract_projects(self, text: str) -> List[Dict[str, Any]]:
        projects = []
        proj_section = self._find_section(text, ['projects', 'personal projects', 'side projects'])
        if not proj_section:
            return projects
        
        lines = proj_section.split('\n')
        current = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if 'github.com' in line or 'http' in line:
                if current:
                    projects.append(current)
                current = {'url': line}
            elif not current:
                current['name'] = line
            elif 'tech' in line.lower() or 'stack' in line.lower():
                current['tech_stack'] = line
            elif line.startswith(('•', '-', '*')):
                bullets = current.setdefault('bullets', [])
                if isinstance(bullets, list):
                    bullets.append(re.sub(r'^[•\-\*]\s*', '', line))
        if current:
            projects.append(current)
        
        return projects
    
    def _find_section(self, text: str, keywords: List[str]) -> Optional[str]:
        text_lower = text.lower()
        start = -1
        for kw in keywords:
            idx = text_lower.find(kw)
            if idx != -1:
                start = idx
                break
        if start == -1:
            return None
        
        next_sections = ['experience', 'skills', 'education', 'projects', 'achievements', 'certifications', 'summary']
        end = len(text)
        for ns in next_sections:
            idx = text_lower.find(ns, start + len(keywords[0]))
            if idx != -1 and idx > start:
                end = min(end, idx)
        
        return text[start:end]
    
    def parse(self, pdf_path: str) -> Dict[str, Any]:
        text = self.extract_text(pdf_path)
        
        return {
            'name': self.extract_name(text),
            'contact': self.extract_contact(text),
            'summary': self.extract_summary(text),
            'skills': self.extract_skills(text),
            'experience': self.extract_experience(text),
            'education': self.extract_education(text),
            'projects': self.extract_projects(text),
        }


def main():
    parser = HybridParser(llm_model="gemma2:2b")
    
    test_dir = Path('/home/ubuntu/corporate/jobhubt/jobborg/experiments/resume_parsing/test_resumes')
    
    for pdf_file in sorted(test_dir.glob('resume_*.pdf')):
        print(f"\n{'='*60}")
        print(f"Parsing: {pdf_file.name}")
        print(f"{'='*60}")
        
        result = parser.parse(str(pdf_file))
        print(json.dumps(result, indent=2, default=str))
        
        out_file = test_dir / f"{pdf_file.stem}_hybrid.json"
        out_file.write_text(json.dumps(result, indent=2))
        print(f"Saved to {out_file}")


if __name__ == '__main__':
    main()