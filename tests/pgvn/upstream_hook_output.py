"""Relocate only legacy hooks.log scratch I/O outside native repository aliases.

Original hook bodies, argv and assertions remain unchanged. No other file or
directory operation is virtualized; an alias never becomes a local directory.
"""
from pathlib import Path
import builtins
import hashlib
import json
import os
import re
import runpy
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()


def install(work,repositories):
    work=Path(work).resolve()
    assert re.fullmatch(r'm6-network-[0-9a-f]{12}',work.parent.name) and work.name=='pgvn'
    assert work.parent.parent==(ROOT/'runtime/pgvn/tests').resolve() and work.is_dir()
    original_open=builtins.open;original_exists=os.path.exists;original_unlink=os.unlink
    def mapped(value,operation):
        if isinstance(value,int):return None
        try:path=Path(os.fsdecode(os.fspath(value))).resolve()
        except (TypeError,ValueError):return None
        if path.name!='hooks.log':return None
        identifier=repositories().get(path.parent)
        if identifier is None:return None
        assert str(uuid.UUID(identifier))==identifier
        assert path.parent.is_relative_to(work) and not path.parent.exists()
        target=(work/'global-hook-output'/identifier/'hooks.log').resolve()
        assert target.is_relative_to(work/'global-hook-output')
        audit=(work/'global-fixture-events').resolve();assert audit.parent==work
        audit.mkdir(exist_ok=True)
        record={'operation':operation,'alias':str(path),'target':str(target),'repository_id':identifier}
        (audit/('hook-output-'+uuid.uuid4().hex+'.json')).write_text(json.dumps(record)+'\n',encoding='utf-8')
        return target
    def opened(file,*args,**kwargs):
        target=mapped(file,'open')
        if target is not None:
            mode=args[0] if args else kwargs.get('mode','r')
            if any(flag in mode for flag in 'wax+'):target.parent.mkdir(parents=True,exist_ok=True)
        return original_open(target if target is not None else file,*args,**kwargs)
    def exists(path):
        target=mapped(path,'exists');return original_exists(target if target is not None else path)
    def unlink(path,*args,**kwargs):
        target=mapped(path,'unlink');return original_unlink(target if target is not None else path,*args,**kwargs)
    builtins.open=opened;os.path.exists=exists;os.unlink=unlink
    def restore():builtins.open=original_open;os.path.exists=original_exists;os.unlink=original_unlink
    return restore


def main():
    context=Path(sys.argv[1]).resolve();script=Path(sys.argv[2]).resolve()
    config=json.loads(context.read_bytes());work=Path(config['work']).resolve()
    suffix=config['database'].removeprefix('pgvn_m6_net_');assert re.fullmatch('[0-9a-f]{12}',suffix)
    assert config['database']=='pgvn_m6_net_'+suffix
    assert work==(ROOT/'runtime/pgvn/tests'/('m6-network-'+suffix)/'pgvn').resolve()
    assert context.is_relative_to(work) and script.is_relative_to(work/'global-hooks')
    assert sha(Path(__file__))==config['hook_output_sha256']
    manifest_path=ROOT/'artifacts/pgvn/build/manifest.json';assert sha(manifest_path)==config['manifest_sha256']
    manifest=json.loads(manifest_path.read_bytes())
    assert sha(Path(__file__))==manifest['sources']['tests/pgvn/upstream_hook_output.py']
    aliases={Path(alias).resolve():identifier for identifier,alias in config['repositories'].items()}
    restore=install(work,lambda:aliases)
    sys.argv=[str(script),*sys.argv[3:]]
    try:runpy.run_path(str(script),run_name='__main__')
    finally:restore()
    return 0


if __name__=='__main__':raise SystemExit(main())
