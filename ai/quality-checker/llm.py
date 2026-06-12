import base64
import difflib
import json
import re

import httpx
from fastapi import HTTPException

from config import VLLM_QWEN_VL_API, MAX_LLM_RETRIES, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT
from models import CriterionInput

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def encode_image_to_base64(image) -> str:
    import cv2
    _, buf = cv2.imencode(".jpg", image)
    return base64.b64encode(buf).decode("utf-8")


def _verdict_from_score(score: int) -> str:
    return "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL")


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_llm_prompt(image_b64: str, criteria: list[CriterionInput]) -> dict:
    quality_criteria = [c for c in criteria if c.type == "quality"]
    feature_criteria = [c for c in criteria if c.type == "feature"]

    sections = []
    if quality_criteria:
        names = "\n".join(f"  - {c.name}" for c in quality_criteria)
        sections.append(
            "QUALITY criteria — score image quality on a 1-10 scale:\n"
            "  Rubric: 1-3 = FAIL, 4-6 = MARGINAL, 7-10 = PASS\n"
            f"{names}"
        )
    if feature_criteria:
        names = "\n".join(f"  - {c.name}" for c in feature_criteria)
        sections.append(
            "FEATURE criteria — detect whether each feature is present:\n"
            "  Rubric: 10 = clearly present (PASS), 5 = uncertain or partially present (MARGINAL), "
            "1 = clearly absent (FAIL)\n"
            "  For each feature criterion, your 'reason' MUST follow this structure:\n"
            "    'I observe [specific visual evidence]. Therefore, [feature] is [present/absent/uncertain].'\n"
            f"{names}"
        )

    criteria_text = "\n\n".join(sections)

    system_prompt = (
        "You are an image assessment expert. "
        "Analyze the provided image against each criterion listed below. "
        "Return ONLY a valid JSON object with this exact structure:\n"
        '{"assessment": {\n'
        '  "overall_verdict": "PASS" | "FAIL" | "MARGINAL",\n'
        '  "overall_score": <1-10>,\n'
        '  "per_criterion_scores": {\n'
        '    "<criterion_name>": {\n'
        '      "score": <1-10>,\n'
        '      "verdict": "PASS" | "FAIL" | "MARGINAL",\n'
        '      "confidence": <0-100>,\n'
        '      "reason": "<concise explanation supporting your score>"\n'
        '    }\n'
        '  }\n'
        '}}\n\n'
        "Use the criterion name exactly as given as the JSON key. "
        "Set confidence to a number 0-100: 0 = completely uncertain, 100 = completely certain."
    )

    return {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": f"{criteria_text}\n\nReturn your assessment as JSON."},
            ]},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }


# ---------------------------------------------------------------------------
# vLLM call with retry
# ---------------------------------------------------------------------------

async def call_vllm(prompt: dict) -> dict:
    """Async vLLM call with retry on parse/structure failures.

    Retries up to MAX_LLM_RETRIES times on JSON decode or missing-key errors.
    HTTP errors from vLLM are raised immediately without retry.
    """
    last_exc: Exception | None = None

    for attempt in range(MAX_LLM_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                response = await client.post(VLLM_QWEN_VL_API, json=prompt)
                response.raise_for_status()
                data = response.json()

            content = data["choices"][0]["message"]["content"]

            # json_object mode guarantees valid JSON; keep regex as fallback
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                json_match = re.search(r"\{[\s\S]*\}", content)
                if not json_match:
                    raise ValueError(f"No JSON found in response: {content[:200]}")
                result = json.loads(json_match.group())

            if "assessment" not in result:
                raise ValueError(f"Response missing 'assessment' key: {content[:200]}")

            return result

        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"vLLM call failed: {exc}")
        except (KeyError, IndexError) as exc:
            raise HTTPException(status_code=502, detail=f"Unexpected vLLM response format: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = exc

    return {
        "assessment": {
            "overall_verdict": "FAIL",
            "overall_score": 1,
            "per_criterion_scores": {
                "_llm_error": {
                    "score": 1,
                    "verdict": "FAIL",
                    "confidence": 0,
                    "reason": f"LLM parsing failed after {MAX_LLM_RETRIES} attempts: {last_exc}",
                }
            },
        }
    }


# ---------------------------------------------------------------------------
# Response validation & normalisation
# ---------------------------------------------------------------------------

def _normalize_criterion_keys(
    per_criterion: dict, criteria: list[CriterionInput]
) -> dict:
    """Fuzzy-match LLM-returned criterion keys back to the requested names."""
    requested_names = [c.name for c in criteria]
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue

        lower = returned_key.lower().strip()
        exact_ci = next((n for n in requested_names if n.lower().strip() == lower), None)
        if exact_ci:
            normalized[exact_ci] = value
            continue

        close = difflib.get_close_matches(returned_key, requested_names, n=1, cutoff=0.6)
        normalized[close[0] if close else returned_key] = value

    return normalized


def validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Clamp scores and confidence to valid ranges, recompute verdicts from scores,
    and normalise criterion keys.
    """
    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        overall_score = 5
    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    per_criterion = _normalize_criterion_keys(
        assessment.get("per_criterion_scores", {}), criteria
    )

    for val in per_criterion.values():
        if not isinstance(val, dict):
            continue
        try:
            score = max(1, min(10, int(val.get("score", 5))))
        except (TypeError, ValueError):
            score = 5
        try:
            confidence = max(0, min(100, int(val.get("confidence", 50))))
        except (TypeError, ValueError):
            confidence = 50
        val["score"] = score
        val["confidence"] = confidence
        val["verdict"] = _verdict_from_score(score)

    assessment["per_criterion_scores"] = per_criterion
    return assessment
