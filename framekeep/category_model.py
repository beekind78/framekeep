"""Local visual-category inference, called only inside the bounded image worker."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

Category = Literal["Photos", "Screenshots", "Illustrations", "Needs review", "Animated"]
CATEGORIES = ("Photos", "Screenshots", "Illustrations", "Needs review", "Animated")
MODEL_VERSION = 2
MODEL_NAME = "clip-vit-base-patch32-int8.onnx"
MODEL_BYTES = 89_117_001
MODEL_SHA256 = "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"
MODEL_COMMIT = "6b7c523703ae26e39be4b03774966f8571c7f523"
MODEL_URL = f"https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/{MODEL_COMMIT}/onnx/vision_model_quantized.onnx"
PROMPTS_SHA256 = "bbc1e3bd4e061e063f26e6753a03dc4eae4891e91858e30d69f8191f88cc82a4"


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
    uncertain = scores[best] < 0.18 or scores[best] - scores[second] < 0.018
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
    if len(raw) > 160_000 or hashlib.sha256(raw).hexdigest() != PROMPTS_SHA256:
        raise ValueError("Category prompt integrity check failed.")
    prompts = json.loads(raw)
    labels = prompts["labels"]
    vectors = np.asarray(prompts["embeddings"], dtype=np.float32)
    if (
        labels != ["Photos", "Screenshots", "Illustrations"]
        or vectors.shape != (3, 6, 512)
        or not np.isfinite(vectors).all()
    ):
        raise ValueError("Invalid category prompts.")
    # Preserve screen edges alongside the usual CLIP center crop for tall images.
    views = [
        ImageOps.pad(image.convert("RGB"), (224, 224), method=Image.Resampling.BICUBIC, color=(123, 117, 104))
    ]
    if max(image.size) / min(image.size) > 1.2:
        views.append(ImageOps.fit(image.convert("RGB"), (224, 224), method=Image.Resampling.BICUBIC))
    mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    pixels = np.stack(
        [((np.asarray(view, dtype=np.float32) / 255 - mean) / std).transpose(2, 0, 1) for view in views]
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 3
    session = ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])
    features = session.run(["image_embeds"], {"pixel_values": pixels})[0]
    if features.shape != (len(views), 512) or not np.isfinite(features).all():
        raise ValueError("Invalid category-model result.")
    features /= np.maximum(np.linalg.norm(features, axis=1, keepdims=True), 1e-9)
    combined = features.mean(axis=0)
    combined /= max(float(np.linalg.norm(combined)), 1e-9)
    return decide_category(np.max(vectors @ combined, axis=1), labels)
