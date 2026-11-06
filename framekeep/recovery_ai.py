"""Pinned CPU super-resolution for small, recovered JPEGs."""

import hashlib
import urllib.request
import uuid
from pathlib import Path

from .output_files import output_directory
from .source_files import open_source

MODEL_NAME = "super-resolution-10.onnx"
MODEL_COMMIT = "395472c4b6b92f788357237983d83fd913cfe7e3"
MODEL_URL = f"https://huggingface.co/onnxmodelzoo/super-resolution-10/resolve/{MODEL_COMMIT}/{MODEL_NAME}"
MODEL_BYTES = 240078
MODEL_SHA256 = "85f36ff88cc504a24af5e0602148bc56a8aa09a58eca8c0da2756f3e8186035e"
MAX_ENHANCE_SIDE = 512
SCALE = 3
MODEL_SIDE = 224
PADDING = 8


def verify_model(data):
    if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("The AI recovery model failed its integrity check.")
    return data


def read_model(path):
    with open_source(path, path.parent) as stream:
        return verify_model(stream.read(MODEL_BYTES + 1))


def recovery_model(data_dir, progress, cancelled):
    with output_directory(data_dir, Path("models/recovery")) as folder:
        path = folder / MODEL_NAME
        try:
            read_model(path)
            return path
        except (OSError, ValueError):
            pass

        progress(0, 0, "Downloading the AI recovery model (240 KB)…")
        with urllib.request.urlopen(MODEL_URL, timeout=20) as response:
            model = verify_model(response.read(MODEL_BYTES + 1))
        if cancelled():
            return None
        temporary = folder / (uuid.uuid4().hex + ".download")
        try:
            with temporary.open("xb") as output:
                output.write(model)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path


def enhance_recovered_image(image, model_path):
    import numpy as np
    import onnxruntime as ort
    from PIL import Image

    width, height = image.size
    if max(width, height) > MAX_ENHANCE_SIDE:
        raise ValueError("This recovered image is too large for AI enlargement.")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    ort.disable_telemetry_events()
    session = ort.InferenceSession(read_model(model_path), sess_options=options, providers=["CPUExecutionProvider"])

    y, cb, cr = image.convert("YCbCr").split()
    stride = MODEL_SIDE - 2 * PADDING
    padded = np.pad(np.asarray(y, dtype=np.float32) / 255, (
        (PADDING, PADDING + (-height % stride)), (PADDING, PADDING + (-width % stride)),
    ), mode="edge")
    output_size = (width * SCALE, height * SCALE)
    output = Image.new("L", output_size)
    edge = PADDING * SCALE
    # Extra pixels around each tile provide context. Only the tile's center is copied back.
    for top in range(0, height, stride):
        for left in range(0, width, stride):
            tile = padded[top:top + MODEL_SIDE, left:left + MODEL_SIDE]
            result = session.run(["output"], {"input": tile[None, None]})[0][0, 0]
            h, w = min(stride, height - top) * SCALE, min(stride, width - left) * SCALE
            center = result[edge:edge + h, edge:edge + w]
            pixels = np.rint(np.clip(center, 0, 1) * 255).astype(np.uint8)
            output.paste(Image.fromarray(pixels), (left * SCALE, top * SCALE))
    color = cb.resize(output_size, Image.Resampling.BICUBIC), cr.resize(output_size, Image.Resampling.BICUBIC)
    return Image.merge("YCbCr", (output, *color)).convert("RGB")
