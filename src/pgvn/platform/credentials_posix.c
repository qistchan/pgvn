/* Linux credential primitives. Validate opened descriptors before reading any
 * key bytes, and never follow symlinks in any path component. */
#ifndef _XOPEN_SOURCE
#define _XOPEN_SOURCE 700
#endif
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <apr_strings.h>
#include "credentials.h"
#include "private/svn_utf_private.h"

void pgvn_secure_zero(void *data,size_t size) { OPENSSL_cleanse(data,size); }

int pgvn_secure_random(unsigned char *data,size_t size)
{
    if(size<=INT_MAX && RAND_priv_bytes(data,(int)size)==1) return 1;
    pgvn_secure_zero(data,size); return 0;
}

int pgvn_utf8_valid(const char *text)
{
    return text && svn_utf__is_valid(text,strlen(text));
}

int pgvn_credential_cipher(int encrypt,const unsigned char key[32],
    const unsigned char *aad,size_t aad_size,const unsigned char nonce[12],
    unsigned char tag[16],const unsigned char *input,size_t size,unsigned char *output,apr_pool_t *pool)
{
    (void)pool;
    int count=0,final=0,valid=0;
    EVP_CIPHER_CTX *context=NULL;
    if(size>INT_MAX || aad_size>INT_MAX) goto done;
    context=EVP_CIPHER_CTX_new();
    if(!context || EVP_CipherInit_ex(context,EVP_aes_256_gcm(),NULL,NULL,NULL,encrypt)!=1
        || EVP_CIPHER_CTX_ctrl(context,EVP_CTRL_GCM_SET_IVLEN,12,NULL)!=1
        || EVP_CipherInit_ex(context,NULL,NULL,key,nonce,encrypt)!=1
        || EVP_CipherUpdate(context,NULL,&count,aad,(int)aad_size)!=1
        || EVP_CipherUpdate(context,output,&count,input,(int)size)!=1) goto done;
    if(!encrypt && EVP_CIPHER_CTX_ctrl(context,EVP_CTRL_GCM_SET_TAG,16,tag)!=1) goto done;
    if(EVP_CipherFinal_ex(context,output+count,&final)!=1 || (size_t)(count+final)!=size) goto done;
    if(encrypt && EVP_CIPHER_CTX_ctrl(context,EVP_CTRL_GCM_GET_TAG,16,tag)!=1) goto done;
    valid=1;
done:
    EVP_CIPHER_CTX_free(context);
    if(!valid) pgvn_secure_zero(output,size);
    return valid;
}

static int directory_valid(int fd,int final)
{
    struct stat info;
    if(fstat(fd,&info) || !S_ISDIR(info.st_mode) || (info.st_uid!=0 && info.st_uid!=geteuid())) return 0;
    if(!(info.st_mode&0022)) return 1;
    /* Root-owned sticky directories such as /tmp are traversal points only.
     * The final key directory must be owned by root/the service and must not
     * be writable by the group or others. */
    return !final && info.st_uid==0 && (info.st_mode&S_ISVTX);
}

static int parent_directory(const char **name,const char *path,apr_pool_t *pool)
{
    if(!path || !*path || strlen(path)>8192) return -1;
    char *copy=apr_pstrdup(pool,path),*cursor=copy;
    int directory=open(path[0]=='/' ? "/" : ".",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    if(directory<0) return -1;
    if(path[0]=='/') ++cursor;
    while(1) {
        char *separator=strchr(cursor,'/');
        if(separator) *separator=0;
        if(!*cursor || !strcmp(cursor,".") || !strcmp(cursor,"..") || !directory_valid(directory,0)) break;
        if(!separator) {
            if(directory_valid(directory,1)) { *name=cursor; return directory; }
            break;
        }
        int next=openat(directory,cursor,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if(next<0) break;
        close(directory); directory=next; cursor=separator+1;
    }
    close(directory); return -1;
}

static int private_file(const struct stat *info)
{
    mode_t mode=info->st_mode&07777;
    return S_ISREG(info->st_mode) && info->st_uid==geteuid() && info->st_nlink==1
        && (mode==0400 || mode==0600);
}

static int same_file(const struct stat *left,const struct stat *right)
{
    return left->st_dev==right->st_dev && left->st_ino==right->st_ino
        && left->st_uid==right->st_uid && left->st_mode==right->st_mode
        && left->st_nlink==right->st_nlink && left->st_size==right->st_size
        && left->st_mtim.tv_sec==right->st_mtim.tv_sec && left->st_mtim.tv_nsec==right->st_mtim.tv_nsec
        && left->st_ctim.tv_sec==right->st_ctim.tv_sec && left->st_ctim.tv_nsec==right->st_ctim.tv_nsec;
}

svn_error_t *pgvn_keyfile_read(const char *path,char *data,size_t capacity,size_t *size,apr_pool_t *pool)
{
    *size=0;
    if(!capacity) return pgvn_keyfile_error();
    pgvn_secure_zero(data,capacity);
    const char *name=NULL;
    int parent=parent_directory(&name,path,pool);
    if(parent<0) return pgvn_keyfile_error();
    int file=openat(parent,name,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
    close(parent);
    if(file<0) return pgvn_keyfile_error();
    struct stat before,after;
    int valid=!fstat(file,&before) && private_file(&before) && before.st_size>0
        && (unsigned long long)before.st_size<capacity;
    size_t received=0;
    while(valid && received<capacity) {
        ssize_t amount=read(file,data+received,capacity-received);
        if(amount<0 && errno==EINTR) continue;
        if(amount<0) { valid=0; break; }
        if(!amount) break;
        received+=(size_t)amount;
    }
    valid=valid && !fstat(file,&after) && same_file(&before,&after) && private_file(&after)
        && received==(size_t)before.st_size && received<capacity;
    if(close(file)) valid=0;
    if(!valid) { pgvn_secure_zero(data,capacity); return pgvn_keyfile_error(); }
    *size=received; return SVN_NO_ERROR;
}

svn_error_t *pgvn_keyfile_write(const char *path,const char *data,size_t size,apr_pool_t *pool)
{
    const char *name=NULL;
    int parent=parent_directory(&name,path,pool);
    if(parent<0) return pgvn_keyfile_error();
    int file=openat(parent,name,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(file<0) {
        int error=errno; close(parent);
        if(error==EEXIST) return svn_error_create(SVN_ERR_FS_ALREADY_EXISTS,NULL,
            "PgVN credential key creation failed; existing files are never replaced");
        return pgvn_keyfile_error();
    }
    struct stat created,after,named;
    int identified=!fstat(file,&created);
    int valid=identified && private_file(&created);
    size_t written=0;
    while(valid && written<size) {
        ssize_t amount=write(file,data+written,size-written);
        if(amount<0 && errno==EINTR) continue;
        if(amount<=0) { valid=0; break; }
        written+=(size_t)amount;
    }
    valid=valid && !fsync(file) && !fstat(file,&after) && private_file(&after) && after.st_size==(off_t)size
        && !fstatat(parent,name,&named,AT_SYMLINK_NOFOLLOW) && same_file(&after,&named);
    if(close(file)) valid=0;
    if(valid && fsync(parent)) valid=0;
    if(!valid && identified && !fstatat(parent,name,&named,AT_SYMLINK_NOFOLLOW)
        && created.st_dev==named.st_dev && created.st_ino==named.st_ino) {
        (void)unlinkat(parent,name,0); (void)fsync(parent);
    }
    close(parent);
    return valid ? SVN_NO_ERROR : pgvn_keyfile_error();
}
