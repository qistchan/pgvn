"""Embed per-process UTF-8 in unsigned Windows x64 executables.

Signed SDK binaries are never rewritten. Existing application manifest fields,
including requested privileges and compatibility, are preserved. This module
uses Windows resource APIs and checks that non-resource sections stay intact.
"""
from pathlib import Path
import ctypes
from ctypes import wintypes
import hashlib
import struct
import xml.etree.ElementTree as ET

ASM1='urn:schemas-microsoft-com:asm.v1'
ASM3='urn:schemas-microsoft-com:asm.v3'
WS2019='http://schemas.microsoft.com/SMI/2019/WindowsSettings'
ET.register_namespace('',ASM1)
ET.register_namespace('asmv3',ASM3)
ET.register_namespace('ws2019',WS2019)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pe_state(data):
    if data[:2]!=b'MZ':raise ValueError('Expected PE image')
    pe=struct.unpack_from('<I',data,0x3c)[0]
    if data[pe:pe+4]!=b'PE\0\0':raise ValueError('Invalid PE signature')
    machine,count=struct.unpack_from('<HH',data,pe+4)
    optional_size=struct.unpack_from('<H',data,pe+20)[0]
    optional=pe+24
    if machine!=0x8664 or struct.unpack_from('<H',data,optional)[0]!=0x20b:raise ValueError('Expected x64 PE32+')
    certificate=struct.unpack_from('<II',data,optional+112+4*8)
    sections={}
    for index in range(count):
        at=optional+optional_size+index*40
        name=data[at:at+8].rstrip(b'\0').decode('ascii')
        size,address,raw_size,raw=struct.unpack_from('<IIII',data,at+8)
        if raw+raw_size>len(data):raise ValueError('Truncated PE section')
        flags=struct.unpack_from('<I',data,at+36)[0]
        if name!='.rsrc':sections[name]={'rva':address,'virtual_size':size,'sha256':digest(data[raw:raw+raw_size]),'flags':flags}
    directory_count=min(16,struct.unpack_from('<I',data,optional+108)[0])
    directories={index:struct.unpack_from('<II',data,optional+112+8*index) for index in range(directory_count)}
    return {'certificate_bytes':certificate[1],'sections':sections,'directories':directories}


class Resources:
    def __init__(self):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HMODULE,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_ssize_t)
        for name,args,result in (
            ('LoadLibraryExW',[wintypes.LPCWSTR,wintypes.HANDLE,wintypes.DWORD],wintypes.HMODULE),
            ('FreeLibrary',[wintypes.HMODULE],wintypes.BOOL),
            ('EnumResourceLanguagesW',[wintypes.HMODULE,ctypes.c_void_p,ctypes.c_void_p,self.callback,ctypes.c_ssize_t],wintypes.BOOL),
            ('FindResourceExW',[wintypes.HMODULE,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD],wintypes.HANDLE),
            ('SizeofResource',[wintypes.HMODULE,wintypes.HANDLE],wintypes.DWORD),
            ('LoadResource',[wintypes.HMODULE,wintypes.HANDLE],wintypes.HANDLE),
            ('LockResource',[wintypes.HANDLE],ctypes.c_void_p),
            ('BeginUpdateResourceW',[wintypes.LPCWSTR,wintypes.BOOL],wintypes.HANDLE),
            ('UpdateResourceW',[wintypes.HANDLE,ctypes.c_void_p,ctypes.c_void_p,wintypes.WORD,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
            ('EndUpdateResourceW',[wintypes.HANDLE,wintypes.BOOL],wintypes.BOOL)):
            method=getattr(self.api,name);method.argtypes=args;method.restype=result

    def manifests(self,path):
        module=self.api.LoadLibraryExW(str(path),None,0x22)
        if not module:raise ctypes.WinError(ctypes.get_last_error())
        languages=[]
        @self.callback
        def collect(module,kind,name,language,argument):
            languages.append(language);return True
        try:
            if not self.api.EnumResourceLanguagesW(module,24,1,collect,0):
                error=ctypes.get_last_error()
                if error not in (1812,1813,1814):raise ctypes.WinError(error)
            result={}
            for language in sorted(languages):
                resource=self.api.FindResourceExW(module,24,1,language)
                if not resource:raise ctypes.WinError(ctypes.get_last_error())
                size=self.api.SizeofResource(module,resource)
                pointer=self.api.LockResource(self.api.LoadResource(module,resource))
                if not pointer:raise ctypes.WinError(ctypes.get_last_error())
                result[language]=ctypes.string_at(pointer,size)
            return result
        finally:
            self.api.FreeLibrary(module)

    def write(self,path,manifests):
        update=self.api.BeginUpdateResourceW(str(path),False)
        if not update:raise ctypes.WinError(ctypes.get_last_error())
        committed=False
        try:
            for language,data in sorted(manifests.items()):
                buffer=ctypes.create_string_buffer(data)
                if not self.api.UpdateResourceW(update,24,1,language,buffer,len(data)):
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self.api.EndUpdateResourceW(update,False):raise ctypes.WinError(ctypes.get_last_error())
            committed=True
        finally:
            if not committed:self.api.EndUpdateResourceW(update,True)


def merge_utf8(data):
    root=ET.fromstring(data)
    if root.tag!='{'+ASM1+'}assembly':raise ValueError('Expected Windows assembly manifest')
    application=root.find('{'+ASM3+'}application')
    if application is None:application=ET.SubElement(root,'{'+ASM3+'}application')
    settings=application.find('{'+ASM3+'}windowsSettings')
    if settings is None:settings=ET.SubElement(application,'{'+ASM3+'}windowsSettings')
    code_page=settings.find('{'+WS2019+'}activeCodePage')
    if code_page is not None and code_page.text=='UTF-8':return data
    if code_page is None:code_page=ET.SubElement(settings,'{'+WS2019+'}activeCodePage')
    code_page.text='UTF-8'
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)+b'\n'


def embed_utf8(path,default_manifest):
    path=Path(path).resolve()
    before=path.read_bytes();state=pe_state(before)
    if state['certificate_bytes']:
        raise ValueError('Refusing to rewrite a signed SDK executable: '+str(path))
    resources=Resources()
    originals=resources.manifests(path)
    selected=originals or {0:Path(default_manifest).read_bytes()}
    merged={language:merge_utf8(data) for language,data in selected.items()}
    if originals!=merged:resources.write(path,merged)
    after=path.read_bytes()
    updated=pe_state(after)
    if set(updated['sections'])!=set(state['sections']):raise ValueError('Resource update changed section set')
    moved={}
    for name,original in state['sections'].items():
        current=updated['sections'][name]
        if any(current[key]!=original[key] for key in ('virtual_size','sha256','flags')):
            raise ValueError('Resource update changed non-resource bytes/flags: '+str(path))
        if current['rva']!=original['rva']:
            # Windows inserts .rsrc before discardable relocation/debug data.
            # Code and writable/read-only program data must keep their RVAs.
            if (original['flags']&0xa0000000) or not original['flags']&0x02000000:
                raise ValueError('Resource update moved program code/data: '+str(path))
            if name!='.reloc' and not name.startswith(('/','.debug')):
                raise ValueError('Unexpected moved discardable section: '+name)
            moved[name]={'before_rva':original['rva'],'after_rva':current['rva']}
    for index,entry in state['directories'].items():
        if index==2:continue
        expected=entry
        if index==5 and '.reloc' in moved:
            expected=(entry[0]+moved['.reloc']['after_rva']-moved['.reloc']['before_rva'],entry[1])
        if updated['directories'].get(index)!=expected:raise ValueError('Resource update changed PE directory '+str(index))
    if resources.manifests(path)!=merged:raise ValueError('Embedded manifest verification failed')
    return {'before_sha256':digest(before),'after_sha256':digest(after),'changed':before!=after,
        'manifest_sha256':{str(language):digest(data) for language,data in merged.items()},
        'non_resource_bytes_unchanged':True,'program_code_data_rvas_unchanged':True,'moved_discardable_sections':moved}
