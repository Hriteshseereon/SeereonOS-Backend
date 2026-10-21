"""AES-256-GCM secret box: byte-compatible with the Node reference (frozen copy: backend/devtools-ts/ref/secretBox.ts) (values sealed by Node open here and
values sealed here open in Node), tamper-evident, key rotation."""

import os
import shutil
import subprocess

import pytest

from app.auth.secret_box import create_secret_box, derive_key
from tests.golden import load_vectors

V = load_vectors('secret_box.json')
K1, K2 = V['keys']


def test_kids_and_node_sealed_values_open():
    assert [derive_key(k)[0] for k in V['keys']] == V['kids']
    box = create_secret_box(K1)
    for s in V['sealed']:
        assert box.open(s['sealed']) == s['plain']
    rotated = create_secret_box(K2, [K1])
    assert all(rotated.open(s['sealed']) == s['plain'] for s in V['sealed'])
    assert create_secret_box(K2).open(V['sealed'][0]['sealed']) is None


def test_tamper_and_plain():
    box = create_secret_box(K1)
    sealed = box.seal('JBSWY3DPEHPK3PXP')
    assert sealed.startswith(f'v1.{derive_key(K1)[0]}:') and 'JBSWY3DPEHPK3PXP' not in sealed
    flipped = sealed[:-2] + ('B' if sealed[-2] == 'A' else 'A') + sealed[-1]
    assert box.open(flipped) is None
    assert box.open('plain:ABC') is None and create_secret_box(K1, allow_plain=True).open('plain:ABC') == 'ABC'
    assert box.open('v1.zz:aa:bb') is None and box.open(123) is None  # type: ignore[arg-type]


_DEVTOOLS = os.path.join(os.path.dirname(__file__), '..', '..', 'devtools-ts', '_bundle.mjs')
_SPEC = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'frontend', 'node_modules', 'esbuild')


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.exists(_DEVTOOLS) or not os.path.exists(_SPEC), reason='node / backend/devtools-ts / frontend node_modules not available'
)
def test_node_opens_python_sealed_values():
    box = create_secret_box(K1)
    sealed = box.seal('GEZDGNBVGY3TQOJQ')
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    code = (
        "import('./backend/devtools-ts/_bundle.mjs').then(async ({ importTs }) => {"
        " const { createSecretBox } = await importTs(null, { name: 'sb', contents: `export { createSecretBox } from './ref/secretBox'` });"
        f" process.stdout.write(String(createSecretBox('{K1}').open('{sealed}'))) }})"
    )
    out = subprocess.run(['node', '-e', code], cwd=root, capture_output=True, text=True, check=True).stdout
    assert out == 'GEZDGNBVGY3TQOJQ'
