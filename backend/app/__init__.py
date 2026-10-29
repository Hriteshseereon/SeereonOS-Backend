"""SeereonOS API package.

Local development convenience: every entry point (uvicorn app.main:app, python -m app.worker / app.cli / app.db.migrate /
app.devtools.*) reads backend/.env.local when it exists, so the same file works on Windows, macOS and Linux without
`set -a; . ./.env.local`. Variables already set in the environment always win, the file can never switch a process to
production (APP_ENV is ignored in it), and it is skipped entirely when APP_ENV=production or under pytest. The file is
git-ignored; production configuration comes only from the hosting provider's environment variables.
"""
import os as _os
import sys as _sys


def _load_local_env() -> None:
    if _os.environ.get('APP_ENV', '').strip().lower() == 'production' or 'pytest' in _sys.modules:
        return
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), '.env.local')
    if not _os.path.isfile(path):
        return
    with open(path, encoding='utf-8-sig') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, sep, value = line.partition('=')
            key, value = key.strip(), value.strip()
            if not sep or not key or key == 'APP_ENV' or key in _os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            _os.environ[key] = value


_load_local_env()
