"""Rebuild generic CLIP label vectors, without reading any user images.

Install requirements-model-build.txt and pass the pinned text-model/tokenizer
directory with --assets. The running app needs only the vision model.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

PROMPTS = {
    "Photos": [
        "a photograph",
        "a camera photograph",
        "a photograph of real people",
        "a photograph of an animal",
        "a photograph of real world scenery",
        "a photograph of food or everyday objects",
    ],
    "Screenshots": [
        "a screenshot of a smartphone screen showing an app interface",
        "a screenshot of a computer desktop or an application window",
        "a screenshot of a website with text, menus and buttons",
        "a screenshot of a chat conversation or a social media app",
        "a screenshot showing a phone status bar and navigation controls",
        "a screenshot of a video game with its user interface",
    ],
    "Illustrations": [
        "a digital illustration or digital artwork",
        "a cartoon, a comic or an anime drawing",
        "a painting, a sketch or a hand drawn picture",
        "an illustrated poster or a graphic design",
        "a drawing of a character or an illustrated scene",
        "a stylized illustration of a landscape",
    ],
}
HASHES = {
    "text_model_quantized.onnx": "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a",
    "tokenizer.json": "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd",
}


def main():
    ort.disable_telemetry_events()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    for name, checksum in HASHES.items():
        if hashlib.sha256((args.assets / name).read_bytes()).hexdigest() != checksum:
            raise ValueError(f"Unexpected build asset: {name}")
    tokenizer = Tokenizer.from_file(str(args.assets / "tokenizer.json"))
    tokenizer.enable_padding(length=77, pad_id=49407, pad_token="<|endoftext|>")
    tokenizer.enable_truncation(max_length=77)
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(args.assets / "text_model_quantized.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    embeddings = []
    for prompts in PROMPTS.values():
        ids = np.asarray([row.ids for row in tokenizer.encode_batch(prompts)], dtype=np.int64)
        features = session.run(["text_embeds"], {"input_ids": ids})[0]
        features /= np.linalg.norm(features, axis=1, keepdims=True)
        embeddings.append([[round(float(value), 8) for value in feature] for feature in features])
    result = {
        "model": "Xenova/clip-vit-base-patch32",
        "commit": "6b7c523703ae26e39be4b03774966f8571c7f523",
        "labels": list(PROMPTS),
        "prompts": PROMPTS,
        "embeddings": embeddings,
    }
    output = Path(__file__).resolve().parents[1] / "framekeep" / "category_prompts.json"
    output.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Category prompt SHA-256:", hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
