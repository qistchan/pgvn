/* Windows CNG and protected-DACL implementation of the platform boundary. */
#include <windows.h>
#include <wchar.h>
#include <limits.h>
#include <bcrypt.h>
#include <aclapi.h>
#include <sddl.h>
#include "credentials.h"

void pgvn_secure_zero(void *data,size_t size) { SecureZeroMemory(data,size); }
int pgvn_secure_random(unsigned char *data,size_t size)
{
    if(size>ULONG_MAX) return 0;
    if(BCryptGenRandom(NULL,data,(ULONG)size,BCRYPT_USE_SYSTEM_PREFERRED_RNG)>=0) return 1;
    pgvn_secure_zero(data,size); return 0;
}
int pgvn_utf8_valid(const char *text)
{
    return text && MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text,-1,NULL,0)>0;
}

static wchar_t *wide_path(const char *path,apr_pool_t *pool)
{
    if(!path || !*path) return NULL;
    int size=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,NULL,0);
    if(!size) return NULL;
    wchar_t *wide=apr_palloc(pool,size*sizeof(*wide));
    return MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,path,-1,wide,size) ? wide : NULL;
}

static svn_boolean_t broad_sid(PSID sid)
{
    const WELL_KNOWN_SID_TYPE kinds[]={WinWorldSid,WinAuthenticatedUserSid,WinBuiltinUsersSid,
        WinBuiltinGuestsSid,WinAnonymousSid,WinNetworkSid,WinInteractiveSid};
    for(size_t i=0;i<sizeof(kinds)/sizeof(kinds[0]);++i) if(IsWellKnownSid(sid,kinds[i])) return TRUE;
    return FALSE;
}

static int key_acl_valid(HANDLE file)
{
    PSECURITY_DESCRIPTOR descriptor=NULL; PACL dacl=NULL;
    DWORD error=GetSecurityInfo(file,SE_FILE_OBJECT,DACL_SECURITY_INFORMATION,NULL,NULL,&dacl,NULL,&descriptor);
    if(error || !descriptor) return 0;
    SECURITY_DESCRIPTOR_CONTROL control=0; DWORD revision=0;
    int valid=dacl && GetSecurityDescriptorControl(descriptor,&control,&revision) && (control&SE_DACL_PROTECTED);
    for(DWORD i=0;valid && i<dacl->AceCount;++i) {
        void *ace=NULL;
        if(!GetAce(dacl,i,&ace)) { valid=0; break; }
        ACE_HEADER *header=ace;
        if(header->AceFlags&INHERIT_ONLY_ACE) continue;
        if(header->AceType==ACCESS_ALLOWED_ACE_TYPE) {
            ACCESS_ALLOWED_ACE *allowed=ace;
            if(allowed->Mask && broad_sid((PSID)&allowed->SidStart)) valid=0;
        } else if(header->AceType!=ACCESS_DENIED_ACE_TYPE) {
            /* Unknown/object/callback allow ACEs require an explicit ACL repair. */
            valid=0;
        }
    }
    LocalFree(descriptor); return valid;
}

svn_error_t *pgvn_keyfile_write(const char *path,const char *data,size_t length,apr_pool_t *pool)
{
    wchar_t *wide=wide_path(path,pool);
    if(!wide || length>ULONG_MAX) return pgvn_keyfile_error();
    HANDLE token=NULL; DWORD size=0;
    if(!OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)) return pgvn_keyfile_error();
    (void)GetTokenInformation(token,TokenUser,NULL,0,&size);
    TOKEN_USER *user=apr_palloc(pool,size);
    BOOL got=GetTokenInformation(token,TokenUser,user,size,&size); CloseHandle(token);
    wchar_t *sid=NULL;
    if(!got || !ConvertSidToStringSidW(user->User.Sid,&sid)) return pgvn_keyfile_error();
    size_t descriptor_length=wcslen(sid)+80;
    wchar_t *sddl=apr_palloc(pool,descriptor_length*sizeof(*sddl));
    swprintf(sddl,descriptor_length,L"D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%ls)",sid); LocalFree(sid);
    PSECURITY_DESCRIPTOR descriptor=NULL;
    if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,SDDL_REVISION_1,&descriptor,NULL)) return pgvn_keyfile_error();
    SECURITY_ATTRIBUTES security={sizeof(security),descriptor,FALSE};
    HANDLE file=CreateFileW(wide,GENERIC_WRITE|READ_CONTROL,0,&security,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
    DWORD error=file==INVALID_HANDLE_VALUE ? GetLastError() : ERROR_SUCCESS;
    LocalFree(descriptor);
    if(file==INVALID_HANDLE_VALUE) return svn_error_create(error==ERROR_FILE_EXISTS || error==ERROR_ALREADY_EXISTS ?
        SVN_ERR_FS_ALREADY_EXISTS : SVN_ERR_AUTHN_FAILED,NULL,"PgVN credential key creation failed; existing files are never replaced");
    DWORD written=0;
    int valid=WriteFile(file,data,(DWORD)length,&written,NULL) && written==(DWORD)length
        && FlushFileBuffers(file) && key_acl_valid(file);
    CloseHandle(file);
    if(!valid) { (void)DeleteFileW(wide); return pgvn_keyfile_error(); }
    return SVN_NO_ERROR;
}

svn_error_t *pgvn_keyfile_read(const char *path,char *data,size_t capacity,size_t *size,apr_pool_t *pool)
{
    *size=0;
    wchar_t *wide=wide_path(path,pool);
    if(!wide || !capacity || capacity>ULONG_MAX) return pgvn_keyfile_error();
    HANDLE file=CreateFileW(wide,GENERIC_READ|READ_CONTROL,FILE_SHARE_READ,NULL,OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_SEQUENTIAL_SCAN,NULL);
    if(file==INVALID_HANDLE_VALUE) return pgvn_keyfile_error();
    BY_HANDLE_FILE_INFORMATION info; DWORD received=0;
    int valid=GetFileInformationByHandle(file,&info) && !(info.dwFileAttributes&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY))
        && !info.nFileSizeHigh && info.nFileSizeLow>0 && info.nFileSizeLow<capacity && key_acl_valid(file)
        && ReadFile(file,data,(DWORD)capacity-1,&received,NULL) && received==info.nFileSizeLow;
    CloseHandle(file);
    if(!valid) { pgvn_secure_zero(data,capacity); return pgvn_keyfile_error(); }
    *size=received; return SVN_NO_ERROR;
}

int pgvn_credential_cipher(int encrypt,const unsigned char key[32],
    const unsigned char *aad,size_t aad_size,const unsigned char nonce[12],
    unsigned char tag[16],const unsigned char *input,size_t size,unsigned char *output,apr_pool_t *pool)
{
    if(size>ULONG_MAX || aad_size>ULONG_MAX) { pgvn_secure_zero(output,size); return 0; }
    BCRYPT_ALG_HANDLE algorithm=NULL; BCRYPT_KEY_HANDLE handle=NULL; ULONG bytes=0,object_size=0;
    NTSTATUS status=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_AES_ALGORITHM,NULL,0);
    if(status>=0) status=BCryptSetProperty(algorithm,BCRYPT_CHAINING_MODE,(PUCHAR)BCRYPT_CHAIN_MODE_GCM,sizeof(BCRYPT_CHAIN_MODE_GCM),0);
    if(status>=0) status=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&object_size,sizeof(object_size),&bytes,0);
    unsigned char *object=apr_pcalloc(pool,object_size ? object_size : 1);
    if(status>=0) status=BCryptGenerateSymmetricKey(algorithm,&handle,object,object_size,(PUCHAR)key,32,0);
    BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO auth; BCRYPT_INIT_AUTH_MODE_INFO(auth);
    auth.pbNonce=(PUCHAR)nonce; auth.cbNonce=12; auth.pbTag=tag; auth.cbTag=16;
    auth.pbAuthData=(PUCHAR)aad; auth.cbAuthData=(ULONG)aad_size;
    if(status>=0) status=encrypt ? BCryptEncrypt(handle,(PUCHAR)input,(ULONG)size,&auth,NULL,0,output,(ULONG)size,&bytes,0)
        : BCryptDecrypt(handle,(PUCHAR)input,(ULONG)size,&auth,NULL,0,output,(ULONG)size,&bytes,0);
    if(handle) BCryptDestroyKey(handle);
    if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    pgvn_secure_zero(object,object_size);
    if(status<0 || bytes!=size) {
        pgvn_secure_zero(output,size);
        return 0;
    }
    return 1;
}
