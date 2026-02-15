"""Local OCR using EasyOCR for reading card text from images."""

from pathlib import Path

import cv2
import numpy as np
import easyocr

# Model files stored in the repo for offline use
_MODEL_DIR = str(Path(__file__).resolve().parent.parent.parent / "models" / "ocr")


def _load_image(image_path: str) -> np.ndarray:
    """Load an image as a numpy array, using PIL as fallback for formats OpenCV can't handle."""
    img = cv2.imread(image_path)
    if img is not None:
        return img
    # PIL handles HEIC, unusual JPEGs, and other formats OpenCV can't
    from PIL import Image
    pil_img = Image.open(image_path)
    pil_img = pil_img.convert("RGB")
    arr = np.array(pil_img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def run_ocr(image_path: str) -> list[str]:
    """
    Run EasyOCR on an image and return extracted text fragments.

    Args:
        image_path: Path to the image file

    Returns:
        List of text strings found in the image
    """
    reader = easyocr.Reader(
        ["en"],
        model_storage_directory=_MODEL_DIR,
        verbose=False,
    )
    img = _load_image(image_path)
    results = reader.readtext(img, detail=0)
    return results


def run_ocr_with_boxes(image_path: str) -> list[dict]:
    """
    Run EasyOCR on an image and return text fragments with bounding boxes.

    Returns:
        List of dicts with keys: text, bbox ({x, y, w, h}), confidence
    """
    reader = easyocr.Reader(
        ["en"],
        model_storage_directory=_MODEL_DIR,
        verbose=False,
    )
    img = _load_image(image_path)
    results = reader.readtext(img, detail=1)
    fragments = []
    for bbox, text, conf in results:
        xs = [float(p[0]) for p in bbox]
        ys = [float(p[1]) for p in bbox]
        fragments.append({
            "text": text,
            "bbox": {
                "x": min(xs),
                "y": min(ys),
                "w": max(xs) - min(xs),
                "h": max(ys) - min(ys),
            },
            "confidence": round(float(conf), 3),
        })
    return fragments
