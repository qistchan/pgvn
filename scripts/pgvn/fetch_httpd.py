"""Fetch the checksum-pinned Apache host source; builds never fetch implicitly."""
from pathlib import Path
import argparse
import hashlib
import json
import urllib.request

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,default=ROOT/'vendor/httpd-2.4.68')
    args=parser.parse_args()
    lock=json.loads((ROOT/'src/pgvn/httpd/source-lock.json').read_text(encoding='utf-8'))
    destination=args.destination.resolve()
    for name,record in lock['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts:raise ValueError('Invalid source path')
        target=destination/name
        if target.exists():data=target.read_bytes()
        else:
            url=record['url']
            if not url.startswith('https://raw.githubusercontent.com/apache/httpd/2.4.68/'):
                raise ValueError('Unexpected source origin')
            with urllib.request.urlopen(url,timeout=30) as response:
                if response.status!=200:raise ValueError('Unexpected source response')
                data=response.read()
        if len(data)!=record['bytes'] or hashlib.sha256(data).hexdigest()!=record['sha256']:
            raise ValueError('Source checksum mismatch: '+name)
        if not target.exists():
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as stream:stream.write(data)
    print(json.dumps({'source_directory':str(destination),'version':lock['version'],'files':len(lock['files'])}))
    return 0


if __name__=='__main__':raise SystemExit(main())
