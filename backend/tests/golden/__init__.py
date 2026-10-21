"""Golden tests: Python ports vs vectors computed by Node from the TypeScript reference (devtools-ts/golden-gen/*.mjs)."""

import json
import os

VECTORS = os.path.join(os.path.dirname(__file__), 'vectors')


def load_vectors(name: str):
    with open(os.path.join(VECTORS, name), encoding='utf-8') as f:
        return json.load(f)
