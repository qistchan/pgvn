"""Read x64 PE imports without loading binaries or relying on an installed SDK."""
from pathlib import Path
import struct

# App-local MSVC runtime DLLs deliberately are not in this OS allowlist.
WINDOWS_DLLS=frozenset('''advapi32 avrt bcrypt bcryptprimitives cabinet cfgmgr32 clbcatq
comctl32 comdlg32 credui crypt32 cryptbase cryptnet cryptsp cryptui d3d11 dbghelp
dhcpcsvc dhcpcsvc6 dnsapi dwmapi dxgi fwpuclnt gdi32 gdi32full hid httpapi
imagehlp imm32 iphlpapi kernel32 kernelbase mpr msasn1 mscoree msctf msimg32
msvcrt mswsock ncrypt netapi32 netutils normaliz nsi ntdll ole32 oleacc oleaut32
powrprof profapi psapi rasapi32 rasman rpcrt4 rsaenh secur32 security setupapi
shell32 shcore shfolder shlwapi sspicli ucrtbase urlmon user32 userenv usp10
uxtheme version wevtapi win32u windowscodecs winhttp wininet winmm winspool.drv
wintrust wkscli wldap32 ws2_32 wtsapi32'''.split())

def is_system_dll(name):
    name=name.lower()
    return name.startswith(('api-ms-win-','ext-ms-win-')) or name.removesuffix('.dll') in WINDOWS_DLLS

def imports(path):
    data=Path(path).read_bytes()
    def value(fmt,offset):
        size=struct.calcsize(fmt)
        if not 0<=offset<=len(data)-size:raise ValueError('Truncated PE structure: '+str(path))
        return struct.unpack_from(fmt,data,offset)
    if data[:2]!=b'MZ':raise ValueError('Not a PE image: '+str(path))
    pe=value('<I',0x3c)[0]
    if data[pe:pe+4]!=b'PE\0\0':raise ValueError('Invalid PE signature: '+str(path))
    machine,count,_,_,_,optional_size,_=value('<HHIIIHH',pe+4)
    optional=pe+24
    if machine!=0x8664 or value('<H',optional)[0]!=0x20b:raise ValueError('Expected Windows x64 image: '+str(path))
    if optional_size<112:raise ValueError('Invalid PE32+ optional header')
    image_base=value('<Q',optional+24)[0]
    headers_size=value('<I',optional+60)[0]
    directories=min(value('<I',optional+108)[0],(optional_size-112)//8)
    sections=[]
    for index in range(count):
        section=optional+optional_size+index*40
        virtual_size,address,raw_size,raw_offset=value('<IIII',section+8)
        sections.append((address,max(virtual_size,raw_size),raw_offset,raw_size))
    def offset(rva,amount=1):
        if 0<=rva<headers_size and rva+amount<=len(data):return rva
        for address,length,raw,raw_size in sections:
            if address<=rva<address+length and rva-address+amount<=raw_size:
                at=raw+rva-address
                if at+amount<=len(data):return at
        raise ValueError('Unmapped PE RVA: '+str(path))
    def name(rva):
        at=offset(rva);end=data.find(b'\0',at,min(at+1024,len(data)))
        if end<0:raise ValueError('Unterminated PE import name')
        result=data[at:end].decode('ascii')
        if not result or '/' in result or '\\' in result:raise ValueError('Invalid import basename')
        return result.lower()
    found=set();delayed=set()
    for directory,record_size in ((1,20),(13,32)):
        if directories<=directory:continue
        rva,size=value('<II',optional+112+8*directory)
        if not rva:continue
        terminated=False
        for index in range(min(4096,size//record_size+1)):
            at=offset(rva+index*record_size,record_size)
            record=value('<'+'I'*(record_size//4),at)
            if not any(record):terminated=True;break
            address=record[3] if directory==1 else record[1]
            if directory==13 and not record[0]&1:address-=image_base
            dependency=name(address);found.add(dependency)
            if directory==13:delayed.add(dependency)
        if not terminated:raise ValueError('Unterminated PE import directory')
    return {'architecture':'x64','imports':sorted(found),'delay_imports':sorted(delayed)}

def closure(entries,search_directories):
    """Return basename -> source and audit, rejecting unbundled non-OS imports."""
    pending=list(map(Path,entries))
    candidates={path.name.lower():path for path in pending}
    for directory in map(Path,search_directories):
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower()=='.dll':candidates.setdefault(path.name.lower(),path)
    files={};audits={};system=set()
    while pending:
        source=pending.pop();key=source.name.lower()
        if key in files:
            if files[key].read_bytes()!=source.read_bytes():raise ValueError('Conflicting binary basename: '+key)
            continue
        audit=imports(source);files[key]=source;audits[key]=audit
        for dependency in audit['imports']:
            if is_system_dll(dependency):system.add(dependency)
            elif dependency in candidates:pending.append(candidates[dependency])
            else:raise ValueError(f'Unbundled dependency {dependency} required by {source}')
    return files,{'images':audits,'system_imports':sorted(system)}
