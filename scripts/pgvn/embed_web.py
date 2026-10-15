"""Embed local monitor assets and the explicit migration identity (no Node/CDN)."""
from pathlib import Path
import hashlib
import json
from embed_migrations import STORAGE, CATALOG


def embed_web(root: Path, generated: Path):
    header = ['/* Generated. */', '#ifndef PGVN_WEB_H', '#define PGVN_WEB_H']
    source = ['/* Generated from web/pgvn-monitor. */', '#include "pgvn_web.h"']
    for filename, symbol in [('index.html', 'index'), ('monitor.css', 'style'), ('monitor.js', 'script')]:
        data = (root / 'web/pgvn-monitor' / filename).read_bytes()
        if not data or len(data) > 262144 or b'\0' in data:
            raise ValueError(f'Invalid or oversized monitor asset: {filename}')
        header.append(f'extern const char pgvn_web_{symbol}[];')
        source.append(f'const char pgvn_web_{symbol}[]={{' + ','.join(map(str, data + b'\0')) + '};')
    for namespace, directory, names in [('STORAGE', root / 'sql', STORAGE), ('CATALOG', root / 'sql/pgvn', CATALOG)]:
        hashes = ','.join(hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names)
        header.append(f'#define PGVN_{namespace}_HASHES ' + json.dumps(hashes))
    header.append('#endif\n')
    (generated / 'pgvn_web.h').write_text('\n'.join(header), encoding='ascii')
    (generated / 'pgvn_web.c').write_text('\n'.join(source) + '\n', encoding='ascii')
