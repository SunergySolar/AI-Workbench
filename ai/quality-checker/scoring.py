from llm import _verdict_from_score


def compute_similarity(example_assessment: dict, input_assessment: dict) -> dict:
    """Compare two LLM assessments per-criterion and return a similarity report."""
    example_scores = example_assessment.get("per_criterion_scores", {})
    input_scores = input_assessment.get("per_criterion_scores", {})
    common = set(example_scores.keys()) & set(input_scores.keys())

    if not common:
        return {"overall_similarity": 0.5, "similarity_score": 5.0,
                "per_criterion": {}, "note": "No common criteria to compare"}

    per_criterion = {}
    for criterion in common:
        e = example_scores[criterion].get("score", 5)
        i = input_scores[criterion].get("score", 5)
        similarity = 1.0 - abs(e - i) / 9.0
        per_criterion[criterion] = {"example_score": e, "input_score": i,
                                    "similarity": round(similarity, 3)}

    overall = sum(v["similarity"] for v in per_criterion.values()) / len(per_criterion)
    return {"overall_similarity": round(overall, 3),
            "similarity_score": round(overall * 10, 1),
            "per_criterion": per_criterion}


def combined_score(input_overall: float, similarity_score: float, weight: float) -> dict:
    """Blend absolute quality score with example-similarity score."""
    score = (1.0 - weight) * input_overall + weight * similarity_score
    score = round(max(1.0, min(10.0, score)), 1)
    return {"score": score, "verdict": _verdict_from_score(int(score))}


def aggregate(scores: list[float], method: str) -> dict:
    """Collapse per-example combined scores into a single aggregate verdict."""
    if method == "min":
        score = min(scores)
    elif method == "max":
        score = max(scores)
    else:
        score = sum(scores) / len(scores)
    score = round(score, 1)
    return {"score": score, "verdict": _verdict_from_score(int(score))}
