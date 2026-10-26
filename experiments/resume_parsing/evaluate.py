#!/usr/bin/env python3
"""
Evaluate resume parsers against ground truth
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Any, Set, Tuple
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class EvalResult:
    field: str
    parser: str
    precision: float
    recall: float
    f1: float
    details: str


def load_ground_truth() -> Dict:
    with open('/home/ubuntu/corporate/jobhubt/jobborg/experiments/resume_parsing/ground_truth.json') as f:
        return json.load(f)


def load_parser_output(parser_name: str, resume_id: str) -> Dict:
    """Load parser output file"""
    test_dir = Path('/home/ubuntu/corporate/jobhubt/jobborg/experiments/resume_parsing/test_resumes')
    
    suffix_map = {
        'rule_based': '',
        'local_llm': '_local_llm',
        'hybrid': '_hybrid'
    }
    
    suffix = suffix_map.get(parser_name, '')
    file_path = test_dir / f"{resume_id}{suffix}.json"
    
    if not file_path.exists():
        return {"error": "File not found"}
    
    with open(file_path) as f:
        return json.load(f)


def normalize_skill(skill: str) -> str:
    """Normalize skill for comparison"""
    return skill.lower().strip().replace('.', '').replace(' ', '')


def evaluate_contact(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate contact info extraction"""
    gt_contact = gt.get('contact', {})
    pred_contact = pred.get('contact', {})
    
    fields = ['email', 'phone', 'linkedin', 'github', 'location']
    tp = fp = fn = 0
    
    for field in fields:
        gt_val = gt_contact.get(field, '').lower().strip()
        pred_val = pred_contact.get(field, '').lower().strip()
        
        if gt_val and pred_val:
            # Fuzzy match for URLs and locations
            if field in ['linkedin', 'github']:
                if gt_val in pred_val or pred_val in gt_val:
                    tp += 1
                else:
                    fp += 1
                    fn += 1
            elif field == 'location':
                # Check if key location components match
                gt_parts = set(gt_val.split())
                pred_parts = set(pred_val.split())
                if gt_parts & pred_parts:
                    tp += 1
                else:
                    fp += 1
                    fn += 1
            else:
                if gt_val == pred_val:
                    tp += 1
                else:
                    fp += 1
                    fn += 1
        elif gt_val:
            fn += 1
        elif pred_val:
            fp += 1
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1


def evaluate_skills(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate skills extraction"""
    gt_skills = {normalize_skill(s) for s in gt.get('skills', [])}
    pred_skills = {normalize_skill(s) for s in pred.get('skills', [])}
    
    # Handle skills with years like "Java (3 years)"
    pred_skills_clean = set()
    for s in pred_skills:
        # Remove years in parentheses
        clean = s.split('(')[0].strip()
        if clean:
            pred_skills_clean.add(clean)
    
    tp = len(gt_skills & pred_skills_clean)
    fp = len(pred_skills_clean - gt_skills)
    fn = len(gt_skills - pred_skills_clean)
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1


def evaluate_experience(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate experience extraction"""
    gt_exp = gt.get('experience', [])
    pred_exp = pred.get('experience', [])
    
    if not gt_exp and not pred_exp:
        return 1.0, 1.0, 1.0
    if not gt_exp:
        return 0.0, 0.0, 0.0
    if not pred_exp:
        return 0.0, 0.0, 0.0
    
    # Match by company name (fuzzy)
    matched = 0
    for gt_e in gt_exp:
        gt_company = gt_e.get('company', '').lower()
        for pred_e in pred_exp:
            pred_company = pred_e.get('company', '').lower()
            if gt_company in pred_company or pred_company in gt_company:
                matched += 1
                break
    
    precision = matched / len(pred_exp) if pred_exp else 0
    recall = matched / len(gt_exp) if gt_exp else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1


def evaluate_education(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate education extraction"""
    gt_edu = gt.get('education', [])
    pred_edu = pred.get('education', [])
    
    if not gt_edu and not pred_edu:
        return 1.0, 1.0, 1.0
    if not gt_edu:
        return 0.0, 0.0, 0.0
    if not pred_edu:
        return 0.0, 0.0, 0.0
    
    # Check if any education entry has school name match
    matched = 0
    for gt_e in gt_edu:
        gt_school = gt_e.get('school', '').lower()
        for pred_e in pred_edu:
            pred_school = pred_e.get('school', '').lower()
            if gt_school and (gt_school in pred_school or pred_school in gt_school):
                matched += 1
                break
            # Also check degree
            gt_degree = gt_e.get('degree', '').lower()
            pred_degree = pred_e.get('degree', '').lower()
            if gt_degree and (gt_degree in pred_degree or pred_degree in gt_degree):
                matched += 1
                break
    
    precision = matched / len(pred_edu) if pred_edu else 0
    recall = matched / len(gt_edu) if gt_edu else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1


def evaluate_projects(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate projects extraction"""
    gt_proj = gt.get('projects', [])
    pred_proj = pred.get('projects', [])
    
    if not gt_proj and not pred_proj:
        return 1.0, 1.0, 1.0
    if not gt_proj:
        return 0.0, 0.0, 0.0
    if not pred_proj:
        return 0.0, 0.0, 0.0
    
    matched = 0
    for gt_p in gt_proj:
        gt_name = gt_p.get('name', '').lower()
        gt_url = gt_p.get('url', '').lower()
        for pred_p in pred_proj:
            pred_name = pred_p.get('name', '').lower()
            pred_url = pred_p.get('url', '').lower()
            if gt_name and (gt_name in pred_name or pred_name in gt_name):
                matched += 1
                break
            if gt_url and pred_url and (gt_url in pred_url or pred_url in gt_url):
                matched += 1
                break
    
    precision = matched / len(pred_proj) if pred_proj else 0
    recall = matched / len(gt_proj) if gt_proj else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
    
    return precision, recall, f1


def evaluate_name(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate name extraction"""
    gt_name = gt.get('name', '').lower().strip()
    pred_name = pred.get('name', '').lower().strip()
    
    if gt_name and pred_name:
        # Check if names share significant parts
        gt_parts = set(gt_name.split())
        pred_parts = set(pred_name.split())
        overlap = gt_parts & pred_parts
        if len(overlap) >= 2 or (len(overlap) == 1 and len(gt_parts) <= 2):
            return 1.0, 1.0, 1.0
        return 0.0, 0.0, 0.0
    elif not gt_name and not pred_name:
        return 1.0, 1.0, 1.0
    else:
        return 0.0, 0.0, 0.0


def evaluate_summary(gt: Dict, pred: Dict) -> Tuple[float, float, float]:
    """Evaluate summary extraction (presence only)"""
    gt_sum = gt.get('summary', '')
    pred_sum = pred.get('summary', '')
    
    if gt_sum and pred_sum:
        return 1.0, 1.0, 1.0
    elif not gt_sum and not pred_sum:
        return 1.0, 1.0, 1.0
    else:
        return 0.0, 0.0, 0.0


def run_evaluation():
    """Run full evaluation"""
    gt_data = load_ground_truth()
    parsers = ['rule_based', 'local_llm', 'hybrid']
    resume_ids = ['resume_1', 'resume_2', 'resume_3', 'resume_4', 'resume_5']
    
    field_evaluators = {
        'name': evaluate_name,
        'contact': evaluate_contact,
        'summary': evaluate_summary,
        'skills': evaluate_skills,
        'experience': evaluate_experience,
        'education': evaluate_education,
        'projects': evaluate_projects,
    }
    
    results = defaultdict(list)
    
    for resume_id in resume_ids:
        gt = gt_data[resume_id]
        print(f"\n{'='*60}")
        print(f"Evaluating {resume_id}")
        print(f"{'='*60}")
        
        for parser in parsers:
            pred = load_parser_output(parser, resume_id)
            
            if "error" in pred:
                print(f"  {parser}: ERROR - {pred['error']}")
                continue
            
            print(f"\n  {parser}:")
            for field, evaluator in field_evaluators.items():
                try:
                    p, r, f1 = evaluator(gt, pred)
                    results[f"{parser}_{field}"].append((p, r, f1))
                    print(f"    {field:15s} P={p:.2f} R={r:.2f} F1={f1:.2f}")
                except Exception as e:
                    print(f"    {field:15s} ERROR: {e}")
                    results[f"{parser}_{field}"].append((0, 0, 0))
    
    # Aggregate results
    print(f"\n{'='*60}")
    print("AGGREGATE RESULTS (Average across 5 resumes)")
    print(f"{'='*60}")
    print(f"{'Parser':<15} {'Field':<15} {'Precision':>10} {'Recall':>10} {'F1':>10}")
    print("-" * 60)
    
    for key, values in sorted(results.items()):
        parser, field = key.split('_', 1)
        avg_p = sum(v[0] for v in values) / len(values)
        avg_r = sum(v[1] for v in values) / len(values)
        avg_f1 = sum(v[2] for v in values) / len(values)
        print(f"{parser:<15} {field:<15} {avg_p:>10.2f} {avg_r:>10.2f} {avg_f1:>10.2f}")
    
    # Summary by parser
    print(f"\n{'='*60}")
    print("PARSER SUMMARY (Average F1 across all fields)")
    print(f"{'='*60}")
    
    parser_scores = defaultdict(list)
    for key, values in results.items():
        parser, _ = key.split('_', 1)
        avg_f1 = sum(v[2] for v in values) / len(values)
        parser_scores[parser].append(avg_f1)
    
    for parser, scores in sorted(parser_scores.items()):
        overall = sum(scores) / len(scores)
        print(f"  {parser:<15} Overall F1: {overall:.2f}")
    
    return results


if __name__ == '__main__':
    run_evaluation()