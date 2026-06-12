import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse

from config import DEFAULT_CRITERIA, LOG_LEVEL
from logger import logger
from models import CompareRequest
from analysis import parse_criteria, analyze_upload, analyze_input, resolve_example
from scoring import compute_similarity, combined_score, aggregate

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s %(funcName)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.info("Starting Document Quality Checker (log level=%s)", LOG_LEVEL)

app = FastAPI(title="Document Quality Checker")


@app.post("/assess")
async def assess_document(
    image: UploadFile,
    criteria: str = Form(
        default=json.dumps(DEFAULT_CRITERIA),
        description=(
            'JSON array of criterion objects. Each object must have "name" (string) and '
            'optionally "type" ("quality" or "feature", default "quality"). '
            'Example: [{"name": "image sharpness", "type": "quality"}, '
            '{"name": "has solar panels", "type": "feature"}]'
        ),
    ),
):
    """Assess a single document image via CV pre-checks + LLM scoring."""
    logger.info("assess_document: filename=%s content_type=%s", image.filename, image.content_type)

    criterion_list = parse_criteria(criteria)
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    result = await analyze_upload(image, criterion_list)
    response = {
        "status": "ok",
        "criteria": [c.model_dump() for c in criterion_list],
        **result,
    }
    logger.info("assess_document: returning status=ok combined_verdict=%s", result["combined_verdict"])
    return JSONResponse(content=response)


@app.post("/assess/compare")
async def assess_with_reference(request: CompareRequest):
    """Assess an input image against one or more reference examples."""
    logger.info("assess_with_reference: %d example(s) aggregation=%s criteria=%s",
                len(request.examples), request.aggregation,
                [c.name for c in request.criteria])

    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    input_task = analyze_input(request.image, request.criteria)
    example_tasks = [resolve_example(ex, request.criteria) for ex in request.examples]

    results = await asyncio.gather(input_task, *example_tasks)
    input_analysis = results[0]
    example_analyses = results[1:]

    input_overall = input_analysis["llm_assessment"].get("overall_score", 5)
    logger.debug("assess_with_reference: input overall_score=%s", input_overall)

    example_results = []
    combined_scores = []

    for i, (example, analysis) in enumerate(zip(request.examples, example_analyses)):
        similarity = compute_similarity(
            analysis["llm_assessment"],
            input_analysis["llm_assessment"],
        )
        cs = combined_score(input_overall, similarity["similarity_score"], example.weight)
        combined_scores.append(cs["score"])
        logger.debug("assess_with_reference: example[%d] weight=%s combined_score=%s verdict=%s",
                     i, example.weight, cs["score"], cs["verdict"])
        example_results.append({
            "index": i,
            "weight": example.weight,
            "pre_generated": example.pre_generated_analysis is not None,
            "example_analysis": analysis,
            "similarity": similarity,
            "combined_score": cs["score"],
            "combined_verdict": cs["verdict"],
        })

    agg = aggregate(combined_scores, request.aggregation)
    logger.info("assess_with_reference: returning aggregate combined_score=%s verdict=%s",
                agg["score"], agg["verdict"])

    return JSONResponse(content={
        "status": "ok",
        "criteria": [c.model_dump() for c in request.criteria],
        "aggregation": request.aggregation,
        "input_analysis": input_analysis,
        "example_results": example_results,
        "aggregate": {
            "method": request.aggregation,
            "combined_score": agg["score"],
            "combined_verdict": agg["verdict"],
            "per_example_combined_scores": combined_scores,
        },
    })


@app.get("/health")
def health():
    logger.debug("health: returning ok")
    return {"status": "ok"}
