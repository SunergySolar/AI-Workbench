import numpy as np

from config import BLUR_THRESHOLD, EXPOSURE_LOW, EXPOSURE_HIGH


def check_blur(image) -> dict:
    """Detect blur using Laplacian variance."""
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
    """Check overall exposure via mean pixel intensity."""
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
