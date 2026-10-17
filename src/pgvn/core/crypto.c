#include "../platform/credentials.h"
#ifdef _WIN32
#include <windows.h>
#include <bcrypt.h>
#include <limits.h>
#include <stdlib.h>
int pgvn_sha256(unsigned char digest[32],const unsigned char *value,size_t length)
{
    if(length>ULONG_MAX) return 0;
    BCRYPT_ALG_HANDLE algorithm=NULL; BCRYPT_HASH_HANDLE hash=NULL;
    DWORD size=0,received=0; unsigned char *object=NULL;
    NTSTATUS status=BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,NULL,0);
    if(!status) status=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&size,sizeof(size),&received,0);
    if(!status && (!size || size>1048576 || !(object=malloc(size)))) status=(NTSTATUS)0xc0000017;
    if(!status) status=BCryptCreateHash(algorithm,&hash,object,size,NULL,0,0);
    if(!status) status=BCryptHashData(hash,(PUCHAR)value,(ULONG)length,0);
    if(!status) status=BCryptFinishHash(hash,digest,32,0);
    if(hash) BCryptDestroyHash(hash);
    if(object) { pgvn_secure_zero(object,size); free(object); }
    if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);
    if(status) pgvn_secure_zero(digest,32);
    return !status;
}
#else
#include <openssl/evp.h>
int pgvn_sha256(unsigned char digest[32],const unsigned char *value,size_t length)
{
    unsigned size=0;
    int ok=EVP_Digest(value,length,digest,&size,EVP_sha256(),NULL)==1 && size==32;
    if(!ok) pgvn_secure_zero(digest,32);
    return ok;
}
#endif
