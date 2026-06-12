"""Document Quality Assessment Service.

Accepts multipart image uploads, runs OpenCV pre-checks, then delegates
to Qwen2.5-VL-7B for criterion-based scoring via the vLLM OpenAI-compatible API.
"""

import asyncio
import base64
import difflib
import json
import os
import re
from typing import Literal, Optional

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VLLM_QWEN_VL_API = os.environ.get(
    "VLLM_QWEN_VL_API", "http://vllm-qwen-vl:8000/v1/chat/completions"
)

BLUR_THRESHOLD = 100.0
EXPOSURE_LOW = 30.0
EXPOSURE_HIGH = 220.0

MIN_IMAGE_WIDTH = 100
MIN_IMAGE_HEIGHT = 100
MAX_LLM_RETRIES = 3

_DEFAULT_CRITERIA = [
    {"name": "document legibility", "type": "quality"},
    {"name": "image sharpness",     "type": "quality"},
    {"name": "proper exposure",     "type": "quality"},
    {"name": "absence of artifacts","type": "quality"},
]

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Document Quality Checker")

http_timeout = httpx.Timeout(120.0, connect=10.0)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CriterionInput(BaseModel):
    name: str = Field(description="The criterion to evaluate.")
    type: Literal["quality", "feature"] = Field(
        default="quality",
        description=(
            "'quality': score image quality 1-10 (1-3=FAIL, 4-6=MARGINAL, 7-10=PASS). "
            "'feature': detect presence/absence (10=clearly present, 5=uncertain, 1=clearly absent)."
        ),
    )


class ImageInput(BaseModel):
    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")


class ExampleInput(BaseModel):
    data: str = Field(description="Base64-encoded image string or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")
    weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How much similarity to this example influences the combined score (0.0–1.0).",
    )
    pre_generated_analysis: Optional[dict] = Field(
        default=None,
        description="A prior analysis result for this example. Providing this skips the LLM call.",
    )


class CompareRequest(BaseModel):
    image: ImageInput
    criteria: list[CriterionInput] = Field(
        default=_DEFAULT_CRITERIA,
        description="List of criteria objects, each with a name and type ('quality' or 'feature').",
    )
    aggregation: Literal["mean", "min", "max"] = Field(
        default="mean",
        description="How to collapse per-example combined scores into a single aggregate verdict.",
    )
    examples: list[ExampleInput] = Field(
        min_length=1,
        description="One or more reference examples to compare the input against.",
    )


# ---------------------------------------------------------------------------
# CV Pre-checks
# ---------------------------------------------------------------------------

def check_blur(image) -> dict:
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return {
        "criterion": "sharpness",
        "score": min(10, int(10 * min(variance / (BLUR_THRESHOLD * 3), 1.0))),
        "verdict": "PASS" if variance >= BLUR_THRESHOLD else "FAIL",
        "confidence": 100,
        "detail": f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
    }


def check_exposure(image) -> dict:
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    mean = float(np.mean(gray))
    if mean < EXPOSURE_LOW:
        return {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                "detail": f"Underexposed (mean: {mean:.1f}, min: {EXPOSURE_LOW})"}
    if mean > EXPOSURE_HIGH:
        return {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                "detail": f"Overexposed (mean: {mean:.1f}, max: {EXPOSURE_HIGH})"}
    score = int(1 + 9 * (mean - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
    return {"criterion": "exposure", "score": score, "verdict": "PASS", "confidence": 100,
            "detail": f"Normal exposure (mean: {mean:.1f})"}


# ---------------------------------------------------------------------------
# LLM scoring
# ---------------------------------------------------------------------------

def encode_image_to_base64(image) -> str:
    import cv2
    _, buf = cv2.imencode(".jpg", image)
    return base64.b64encode(buf).decode("utf-8")


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


async def call_vllm(prompt: dict) -> dict:
    """Async vLLM call with retry on parse/structure failures.

    Retries up to MAX_LLM_RETRIES times on JSON decode or missing-key errors.
    HTTP errors from vLLM are raised immediately without retry.
    """
    last_exc: Exception | None = None

    for attempt in range(MAX_LLM_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
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
            # Retry on parse / structure errors

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

def _verdict_from_score(score: int) -> str:
    return "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL")


def _normalize_criterion_keys(
    per_criterion: dict, criteria: list[CriterionInput]
) -> dict:
    """Fuzzy-match LLM-returned criterion keys back to the requested names.

    The LLM sometimes returns 'Solar Panels' instead of 'has solar panels', or
    truncates / capitalises differently. This resolves those mismatches silently.
    """
    requested_names = [c.name for c in criteria]
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        # 1. Exact match
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue

        # 2. Case-insensitive match
        lower = returned_key.lower().strip()
        exact_ci = next((n for n in requested_names if n.lower().strip() == lower), None)
        if exact_ci:
            normalized[exact_ci] = value
            continue

        # 3. Fuzzy match (cutoff 0.6 keeps it strict enough to avoid false matches)
        close = difflib.get_close_matches(returned_key, requested_names, n=1, cutoff=0.6)
        normalized[close[0] if close else returned_key] = value

    return normalized


def _validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Clamp scores and confidence to valid ranges, recompute verdicts from scores,
    and normalise criterion keys.
    """
    # Overall score & verdict
    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        overall_score = 5
    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    # Per-criterion scores
    per_criterion = assessment.get("per_criterion_scores", {})
    per_criterion = _normalize_criterion_keys(per_criterion, criteria)

    for key, val in per_criterion.items():
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


# ---------------------------------------------------------------------------
# Image loading & pre-validation
# ---------------------------------------------------------------------------

def _validate_image_dimensions(w: int, h: int) -> None:
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Image too small ({w}×{h} px). "
                f"Minimum is {MIN_IMAGE_WIDTH}×{MIN_IMAGE_HEIGHT} px."
            ),
        )


async def _bytes_to_bgr(raw: bytes):
    import cv2
    nparr = np.frombuffer(raw, np.uint8)
    bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=400, detail="Failed to decode image")
    return bgr


async def _load_bgr_from_input(data: str, type_: str):
    if type_ == "base64":
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "QualityChecker/1.0"})
                r.raise_for_status()
                raw = r.content
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch image URL: {exc}")
    return await _bytes_to_bgr(raw)


# ---------------------------------------------------------------------------
# Criteria parsing helper (for multipart /assess endpoint)
# ---------------------------------------------------------------------------

def _parse_criteria(raw: str) -> list[CriterionInput]:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("criteria must be a JSON array")
        return [CriterionInput(**item) for item in parsed]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid criteria JSON: {exc}. "
                "Expected a JSON array of objects, e.g. "
                '[{"name": "image sharpness", "type": "quality"}]'
            ),
        )


# ---------------------------------------------------------------------------
# Core analysis (shared by all endpoints)
# ---------------------------------------------------------------------------

async def _analyze_bgr(
    image_bgr,
    original_w: int,
    original_h: int,
    content_type: str,
    size_bytes: int,
    criteria: list[CriterionInput],
) -> dict:
    import cv2

    # Pre-validate dimensions
    _validate_image_dimensions(original_w, original_h)

    max_dim = 1000
    if max(original_h, original_w) > max_dim:
        scale = max_dim / max(original_h, original_w)
        image_bgr = cv2.resize(
            image_bgr, (int(original_w * scale), int(original_h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    cv_results = {
        "sharpness": check_blur(image_bgr),
        "exposure": check_exposure(image_bgr),
    }

    image_b64 = encode_image_to_base64(image_bgr)
    llm_result = await call_vllm(build_llm_prompt(image_b64, criteria))

    assessment = llm_result.get("assessment", {})

    # Validate, clamp, and normalise the LLM response
    assessment = _validate_and_clamp(assessment, criteria)

    per_criterion = assessment.get("per_criterion_scores", {})

    # Merge CV results for criteria not already scored by the LLM
    for cv_name, cv_result in cv_results.items():
        if cv_name not in per_criterion:
            per_criterion[cv_name] = cv_result

    cv_failures = sum(1 for r in cv_results.values() if r["verdict"] == "FAIL")
    cv_verdict = "FAIL" if cv_failures >= 2 else ("MARGINAL" if cv_failures == 1 else "PASS")

    return {
        "image_info": {
            "width": original_w, "height": original_h,
            "format": content_type, "size_bytes": size_bytes,
        },
        "cv_pre_checks": cv_results,
        "cv_overall_verdict": cv_verdict,
        "llm_assessment": assessment,
        "combined_verdict": cv_verdict if not assessment.get("overall_verdict") else assessment["overall_verdict"],
    }


async def _analyze_upload(upload: UploadFile, criteria: list[CriterionInput]) -> dict:
    if not upload.content_type or upload.content_type.split("/")[1] not in ("jpeg", "jpg", "png"):
        raise HTTPException(status_code=400, detail=f"Only JPEG/PNG accepted (got {upload.content_type})")
    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file")
    bgr = await _bytes_to_bgr(contents)
    h, w = bgr.shape[:2]
    return await _analyze_bgr(bgr, w, h, upload.content_type, len(contents), criteria)


async def _analyze_input(img: ImageInput, criteria: list[CriterionInput]) -> dict:
    bgr = await _load_bgr_from_input(img.data, img.type)
    h, w = bgr.shape[:2]
    size = len(base64.b64decode(img.data)) if img.type == "base64" else 0
    return await _analyze_bgr(bgr, w, h, "image/jpeg", size, criteria)


async def _resolve_example(example: ExampleInput, criteria: list[CriterionInput]) -> dict:
    if example.pre_generated_analysis is not None:
        return example.pre_generated_analysis
    return await _analyze_input(ImageInput(data=example.data, type=example.type), criteria)


# ---------------------------------------------------------------------------
# Similarity & scoring helpers
# ---------------------------------------------------------------------------

def _compute_similarity(example_assessment: dict, input_assessment: dict) -> dict:
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


def _combined_score(input_overall: float, similarity_score: float, weight: float) -> dict:
    score = (1.0 - weight) * input_overall + weight * similarity_score
    score = round(max(1.0, min(10.0, score)), 1)
    return {"score": score, "verdict": _verdict_from_score(int(score))}


def _aggregate(scores: list[float], method: str) -> dict:
    if method == "min":
        score = min(scores)
    elif method == "max":
        score = max(scores)
    else:
        score = sum(scores) / len(scores)
    score = round(score, 1)
    return {"score": score, "verdict": _verdict_from_score(int(score))}


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.post("/assess")
async def assess_document(
    image: UploadFile,
    criteria: str = Form(
        default=json.dumps(_DEFAULT_CRITERIA),
        description=(
            'JSON array of criterion objects. Each object must have "name" (string) and '
            'optionally "type" ("quality" or "feature", default "quality"). '
            'Example: [{"name": "image sharpness", "type": "quality"}, '
            '{"name": "has solar panels", "type": "feature"}]'
        ),
    ),
):
    """Assess a single document image via CV pre-checks + LLM scoring."""
    criterion_list = _parse_criteria(criteria)
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")
    result = await _analyze_upload(image, criterion_list)
    return JSONResponse(content={
        "status": "ok",
        "criteria": [c.model_dump() for c in criterion_list],
        **result,
    })


@app.post("/assess/compare")
async def assess_with_reference(request: CompareRequest):
    """Assess an input image against one or more reference examples."""
    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    input_task = _analyze_input(request.image, request.criteria)
    example_tasks = [_resolve_example(ex, request.criteria) for ex in request.examples]

    results = await asyncio.gather(input_task, *example_tasks)
    input_analysis = results[0]
    example_analyses = results[1:]

    input_overall = input_analysis["llm_assessment"].get("overall_score", 5)

    example_results = []
    combined_scores = []

    for i, (example, analysis) in enumerate(zip(request.examples, example_analyses)):
        similarity = _compute_similarity(
            analysis["llm_assessment"],
            input_analysis["llm_assessment"],
        )
        combined = _combined_score(input_overall, similarity["similarity_score"], example.weight)
        combined_scores.append(combined["score"])
        example_results.append({
            "index": i,
            "weight": example.weight,
            "pre_generated": example.pre_generated_analysis is not None,
            "example_analysis": analysis,
            "similarity": similarity,
            "combined_score": combined["score"],
            "combined_verdict": combined["verdict"],
        })

    aggregate = _aggregate(combined_scores, request.aggregation)

    return JSONResponse(content={
        "status": "ok",
        "criteria": [c.model_dump() for c in request.criteria],
        "aggregation": request.aggregation,
        "input_analysis": input_analysis,
        "example_results": example_results,
        "aggregate": {
            "method": request.aggregation,
            "combined_score": aggregate["score"],
            "combined_verdict": aggregate["verdict"],
            "per_example_combined_scores": combined_scores,
        },
    })


@app.get("/health")
def health():
    return {"status": "ok"}
