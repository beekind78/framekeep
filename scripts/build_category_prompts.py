"""Rebuild generic SigLIP 2 label vectors, without reading any user images.

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
        "this is a camera photograph of a real scene.",
        "this is a candid mobile phone photo of people or a family gathering.",
        "this is a photograph of real scenery, trees, or buildings.",
        "this is a close-up photo of a pet, a meal, or an everyday object.",
        "this is a photograph taken indoors under natural lighting.",
        "this is an unedited travel photo taken outdoors with a phone camera.",
        "this is a camera photo of a printed document or product on a table.",
        "this is a photograph of a real place with photographic texture and lighting.",
    ],
    "Screenshots": [
        "this is a screenshot of a smartphone app with a status bar and navigation buttons.",
        "this is a screenshot of a messaging conversation with chat bubbles and timestamps.",
        "this is a screenshot of a desktop application window or computer settings.",
        "this is a screenshot of a website with menus, buttons, and text.",
        "this is a screenshot of a social media feed containing photos or drawings.",
        "this is a screenshot of a mobile map, menu, ticket, or order screen.",
        "this is a screenshot of a video game with an on-screen interface.",
        "this is a screen capture showing a photo or artwork inside an app interface.",
    ],
    "Illustrations": [
        "this is a digital illustration of a character or scene.",
        "this is an anime drawing or cartoon character with clean outlines.",
        "this is a comic panel with illustrated characters and speech balloons.",
        "this is a small cartoon sticker or a simple drawing on a plain background.",
        "this is a hand-drawn sketch, watercolor painting, or original artwork.",
        "this is a stylized illustrated landscape or a digitally painted background.",
        "this is a flat vector graphic or an illustrated poster.",
        "this is a standalone cartoon or character illustration with no app interface.",
    ],
}
HASHES = {
    "text_model_quantized.onnx": "3a0603d3a00c05a80a6ded4743c16aaac7b1e62cdcc7e362e7ce418659b96400",
    "tokenizer.json": "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322",
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
    tokenizer.enable_padding(length=64, pad_id=0, pad_token="<pad>")
    tokenizer.enable_truncation(max_length=64)
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
        features = session.run(["pooler_output"], {"input_ids": ids})[0]
        features /= np.linalg.norm(features, axis=1, keepdims=True)
        embeddings.append([[round(float(value), 8) for value in feature] for feature in features])
    result = {
        "model": "onnx-community/siglip2-base-patch16-224-ONNX",
        "commit": "ba1f3b0843f24bc5417d38e19c37b287d719b2f4",
        "labels": list(PROMPTS),
        "prompts": PROMPTS,
        "embeddings": embeddings,
    }
    output = Path(__file__).resolve().parents[1] / "framekeep" / "category_prompts.json"
    output.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
    print("Category prompt SHA-256:", hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
