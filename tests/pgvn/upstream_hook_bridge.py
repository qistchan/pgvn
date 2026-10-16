"""Test-only translation from PgVN JSON events to original Python hook argv.

The supplied upstream Python hook body is written unchanged outside repository
aliases. This does not make production PgVN execute legacy repository scripts.
"""
from pathlib import Path
import base64
import hashlib
import json
import os
import re
import subprocess
import sys

def main():
    config_path=Path(sys.argv[1]).resolve();script=Path(sys.argv[2]).resolve()
    config=json.loads(config_path.read_text(encoding='utf-8'));work=Path(config['work']).resolve()
    assert re.fullmatch(r'pgvn_m6_net_[0-9a-f]{12}',config['database'])
    assert config_path.is_relative_to(work) and script.is_relative_to(work/'global-hooks')
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==config['bridge_sha256']
    event=json.loads(sys.stdin.buffer.read());kind=event['event'];repo=event['repository_id']
    assert os.environ['PGVN_EVENT']==kind and os.environ['PGVN_REPOSITORY_ID']==repo
    alias=config['repositories'][repo];user=event.get('username') or ''
    revision=str(event.get('revision') if event.get('revision') is not None else '')
    txn=event.get('transaction_id') or '';path=event.get('path') or ''
    force='1' if event.get('force') else '0';args=[alias];stdin=b''
    if kind=='start-commit':args += [user,':'.join(event.get('capabilities') or []),txn]
    elif kind=='pre-commit':args += [txn]
    elif kind=='post-commit':args += [revision,txn]
    elif kind in ('pre-revprop-change','post-revprop-change'):
        args += [revision,user,event.get('property_name') or '',event.get('action') or '']
        stdin=base64.b64decode(event.get('property_value_base64') or '',validate=True)
    elif kind=='pre-lock':args += [path,user,event.get('lock_comment') or '',force]
    elif kind=='pre-unlock':args += [path,user,event.get('lock_token') or '',force]
    elif kind in ('post-lock','post-unlock'):
        args += [user];stdin=('\n'.join(event.get('paths') or [])+'\n').encode('utf-8')
    else:raise ValueError('Unsupported hook event: '+kind)
    env={k:v for k,v in os.environ.items() if not k.startswith('PGVN_')}
    env.update(PGVN_DSN=f"host=127.0.0.1 port=16432 dbname={config['database']} user=pgvn_admin sslmode=disable",
               PGVN_UPSTREAM_DATABASE=config['database'],PGVN_UPSTREAM_ROOT=work.as_posix(),
               PGVN_UPSTREAM_LEDGER=(work/'native-commands.jsonl').as_posix(),LC_ALL='C')
    runner=Path(__file__).with_name('upstream_hook_output.py')
    assert hashlib.sha256(runner.read_bytes()).hexdigest()==config['hook_output_sha256']
    result=subprocess.run([sys.executable,'-I','-B',str(runner),str(config_path),str(script),*args],cwd=work,env=env,input=stdin,
                          creationflags=subprocess.CREATE_NO_WINDOW,timeout=25)
    return result.returncode

if __name__=='__main__':raise SystemExit(main())
