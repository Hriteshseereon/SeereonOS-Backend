"""The committed catalogue JSON (app/catalog/data) equals a fresh export from the TypeScript sources, and the
collation weights match the Node ICU they were derived from."""

import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
# the export needs the TypeScript spec (frontend/src) and the dev tools (backend/devtools-ts): not part of the backend
# deliverable, so the check is skipped there
HAVE_SPEC = os.path.isdir(os.path.join(ROOT, 'frontend', 'src', 'mock')) and os.path.isdir(os.path.join(ROOT, 'frontend', 'node_modules', 'esbuild'))
HAVE_TOOLS = os.path.exists(os.path.join(ROOT, 'backend', 'devtools-ts', 'export_catalog.mjs'))


@pytest.mark.skipif(not (shutil.which('node') and HAVE_SPEC and HAVE_TOOLS), reason='node / TypeScript spec / devtools-ts not available')
def test_catalog_is_up_to_date():
    r = subprocess.run(['node', 'backend/devtools-ts/export_catalog.mjs', '--check'], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_collation_weights_match_node_icu():
    with open(os.path.join(ROOT, 'backend/app/js_collation.json'), encoding='utf-8') as f:
        icu = json.load(f)['icu']
    out = subprocess.run(['node', '-p', 'process.versions.icu'], capture_output=True, text=True, check=True).stdout.strip()
    assert out == icu, f'Node ICU {out} != {icu}: run node backend/devtools-ts/gen_collation.mjs and the js golden tests'
