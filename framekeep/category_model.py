"""Local visual-category inference, called only inside the bounded image worker."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, get_args

Category = Literal["Photos", "Screenshots", "Illustrations", "Downloads", "Needs review", "Animated"]
CATEGORIES = get_args(Category)
MODEL_VERSION = 3
MODEL_NAME = "siglip2-base-patch16-224-int8.onnx"
MODEL_BYTES = 94_553_333
MODEL_SHA256 = "5f2b401c1a4fc095702a5d45348e17ad46c4f87064085365b43c6e8eaa5c0070"
MODEL_COMMIT = "ba1f3b0843f24bc5417d38e19c37b287d719b2f4"
MODEL_URL = (
    f"https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX/resolve/{MODEL_COMMIT}"
    "/onnx/vision_model_quantized.onnx?download=true"
)
PROMPTS_SHA256 = "a8836254965ec247aa675e321b527cec54410a4046114ec605db818aab98c322"


def initial_category(path: Path, camera: str, animated: bool) -> dict:
    if animated:
        kind, source = "Animated", "animation"
    elif re.search(r"screen[\s_-]?(shot|capture)", path.name, re.IGNORECASE):
        kind, source = "Screenshots", "filename"
    elif camera:
        kind, source = "Photos", "camera"
    else:
        kind, source = "Needs review", "pending"
    return {
        "kind": kind,
        "kind_source": source,
        "kind_score": None,
        "kind_suggestion": kind,
        "category_version": 0,
    }


def decide_category(similarities, labels) -> dict:
    """Close matches remain for review; strength is not an accuracy estimate."""
    import numpy as np

    scores = np.asarray(similarities, dtype=np.float32)
    if scores.shape != (3,) or not np.isfinite(scores).all():
        raise ValueError("Invalid category-model scores.")
    order = np.argsort(scores)
    best, second = int(order[-1]), int(order[-2])
    weights = np.exp((scores - scores[best]) * 100)
    uncertain = scores[best] < 0.08 or scores[best] - scores[second] < 0.025
    return {
        "kind": "Needs review" if uncertain else labels[best],
        "kind_suggestion": labels[best],
        "kind_score": float(weights[best] / weights.sum()),
        "kind_source": "uncertain" if uncertain else "model",
        "category_version": MODEL_VERSION,
    }


def predict_category(image, directory: Path) -> dict:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageOps

    ort.disable_telemetry_events()
    with (directory / MODEL_NAME).open("rb") as stream:
        model = stream.read(MODEL_BYTES + 1)
    if len(model) != MODEL_BYTES or hashlib.sha256(model).hexdigest() != MODEL_SHA256:
        raise ValueError("Category model integrity check failed. Download it again.")
    raw = Path(__file__).with_name("category_prompts.json").read_bytes()
    if len(raw) > 320_000 or hashlib.sha256(raw).hexdigest() != PROMPTS_SHA256:
        raise ValueError("Category prompt integrity check failed.")
    prompts = json.loads(raw)
    labels = prompts["labels"]
    vectors = np.asarray(prompts["embeddings"], dtype=np.float32)
    if (
        labels != ["Photos", "Screenshots", "Illustrations"]
        or vectors.shape != (3, 8, 768)
        or not np.isfinite(vectors).all()
    ):
        raise ValueError("Invalid category prompts.")
    # The full frame preserves status bars, menus and other screenshot cues.
    rgb = image.convert("RGB")
    views = [rgb.resize((224, 224), Image.Resampling.BILINEAR)]
    if max(rgb.size) / min(rgb.size) > 1.2:
        views.append(ImageOps.fit(rgb, (224, 224), method=Image.Resampling.BILINEAR))
    pixels = np.stack([(np.asarray(view, dtype=np.float32) / 127.5 - 1).transpose(2, 0, 1) for view in views])
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 3
    session = ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])
    features = session.run(["pooler_output"], {"pixel_values": pixels})[0]
    if features.shape != (len(views), 768) or not np.isfinite(features).all():
        raise ValueError("Invalid category-model result.")
    features /= np.maximum(np.linalg.norm(features, axis=1, keepdims=True), 1e-9)
    combined = np.average(features, axis=0, weights=[.8, .2] if len(views) == 2 else None)
    combined /= max(float(np.linalg.norm(combined)), 1e-9)
    # Use several agreeing prompts so a single matching phrase cannot decide the category.
    return decide_category(np.sort(vectors @ combined, axis=1)[:, -3:].mean(axis=1), labels)
