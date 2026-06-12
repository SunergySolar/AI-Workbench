import base64
import json

import httpx
import numpy as np
from fastapi import HTTPException, UploadFile

from config import MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT
from models import CriterionInput, ImageInput, ExampleInput
from cv import check_blur, check_exposure
from llm import encode_image_to_base64, build_llm_prompt, call_vllm, validate_and_clamp

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# Criteria parsing
# ---------------------------------------------------------------------------

def parse_criteria(raw: str) -> list[CriterionInput]:
    """Parse a JSON string into a validated list of CriterionInput objects."""
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
# Image loading
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
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "QualityChecker/1.0"})
                r.raise_for_status()
                raw = r.content
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch image URL: {exc}")
    return await _bytes_to_bgr(raw)


# ---------------------------------------------------------------------------
# Core analysis pipeline
# ---------------------------------------------------------------------------

async def analyze_bgr(
    image_bgr,
    original_w: int,
    original_h: int,
    content_type: str,
    size_bytes: int,
    criteria: list[CriterionInput],
) -> dict:
    import cv2

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

    assessment = validate_and_clamp(llm_result.get("assessment", {}), criteria)

    per_criterion = assessment.get("per_criterion_scores", {})
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


async def analyze_upload(upload: UploadFile, criteria: list[CriterionInput]) -> dict:
    if not upload.content_type or upload.content_type.split("/")[1] not in ("jpeg", "jpg", "png"):
        raise HTTPException(status_code=400, detail=f"Only JPEG/PNG accepted (got {upload.content_type})")
    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file")
    bgr = await _bytes_to_bgr(contents)
    h, w = bgr.shape[:2]
    return await analyze_bgr(bgr, w, h, upload.content_type, len(contents), criteria)


async def analyze_input(img: ImageInput, criteria: list[CriterionInput]) -> dict:
    bgr = await _load_bgr_from_input(img.data, img.type)
    h, w = bgr.shape[:2]
    size = len(base64.b64decode(img.data)) if img.type == "base64" else 0
    return await analyze_bgr(bgr, w, h, "image/jpeg", size, criteria)


async def resolve_example(example: ExampleInput, criteria: list[CriterionInput]) -> dict:
    if example.pre_generated_analysis is not None:
        return example.pre_generated_analysis
    return await analyze_input(ImageInput(data=example.data, type=example.type), criteria)
