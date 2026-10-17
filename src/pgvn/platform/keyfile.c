#include "platform_internal.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int pgvn_key_stat_ok(const struct stat *info, uid_t owner)
{
    if (!info || !S_ISREG(info->st_mode)) return 0;
    if (info->st_uid != owner || info->st_nlink != 1) return 0;
    return (info->st_mode & 0777) == 0600;
}

static int valid_id(const char *id)
{
    size_t length, i;
    if (!id || !*id) return 0;
    length = strlen(id);
    if (length > 64) return 0;
    for (i = 0; i < length; ++i) {
        unsigned char c = (unsigned char)id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
            || c == '_' || c == '-' || c == '.')) return 0;
    }
    return 1;
}

static void hex_encode(char *out, const unsigned char *in, size_t size)
{
    static const char digits[] = "0123456789abcdef";
    size_t i;
    for (i = 0; i < size; ++i) {
        out[i * 2] = digits[in[i] >> 4];
        out[i * 2 + 1] = digits[in[i] & 15];
    }
    out[size * 2] = 0;
}

static int hex_decode(unsigned char *out, const char *in, size_t size)
{
    size_t i;
    if (strlen(in) != size * 2) return 0;
    for (i = 0; i < size; ++i) {
        unsigned value = 0;
        int nibble;
        for (nibble = 0; nibble < 2; ++nibble) {
            unsigned char c = (unsigned char)in[i * 2 + nibble];
            if (c >= '0' && c <= '9') value = value * 16u + (unsigned)(c - '0');
            else if (c >= 'a' && c <= 'f') value = value * 16u + (unsigned)(c - 'a' + 10);
            else return 0;
        }
        out[i] = (unsigned char)value;
    }
    return 1;
}

static int write_all(int fd, const char *data, size_t size)
{
    size_t done = 0;
    while (done < size) {
        ssize_t wrote = write(fd, data + done, size - done);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) return -1;
        done += (size_t)wrote;
    }
    return 0;
}

int pgvn_key_create(const char *path, const char *key_id)
{
    int parent = -1, file = -1, code;
    char name[256], secret_hex[65], text[160];
    unsigned char secret[PGVN_AES_KEY];
    struct stat info;
    int count;
    if (!valid_id(key_id)) return PGVN_PLATFORM_INVALID;
    code = pgvn_open_parent(path, &parent, name, sizeof(name));
    if (code) return code;
    file = openat(parent, name, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (file < 0) {
        code = errno == EEXIST ? PGVN_PLATFORM_EXISTS : errno == ELOOP ? PGVN_PLATFORM_DENIED : PGVN_PLATFORM_IO;
        close(parent);
        return code;
    }
    if (fstat(file, &info) || !pgvn_key_stat_ok(&info, geteuid()) || info.st_size != 0
        || pgvn_random_bytes(secret, sizeof(secret))) {
        code = PGVN_PLATFORM_DENIED;
        goto fail;
    }
    hex_encode(secret_hex, secret, sizeof(secret));
    count = snprintf(text, sizeof(text), "PGVNKEY1\n%s\n%s\n", key_id, secret_hex);
    if (count < 0 || (size_t)count >= sizeof(text) || write_all(file, text, (size_t)count) || fsync(file)
        || fstat(file, &info) || info.st_size != count || !pgvn_key_stat_ok(&info, geteuid())) {
        code = PGVN_PLATFORM_IO;
        goto fail;
    }
    pgvn_secure_zero(secret, sizeof(secret));
    pgvn_secure_zero(secret_hex, sizeof(secret_hex));
    pgvn_secure_zero(text, sizeof(text));
    close(file);
    close(parent);
    return PGVN_PLATFORM_OK;
fail:
    pgvn_secure_zero(secret, sizeof(secret));
    pgvn_secure_zero(secret_hex, sizeof(secret_hex));
    pgvn_secure_zero(text, sizeof(text));
    if (file >= 0) close(file);
    unlinkat(parent, name, 0);
    close(parent);
    return code;
}

int pgvn_key_load(pgvn_loaded_key *key, const char *path)
{
    int parent = -1, file = -1, code;
    char name[256], text[160];
    struct stat info;
    ssize_t got;
    char *id, *encoded, *newline;
    if (!key) return PGVN_PLATFORM_INVALID;
    pgvn_secure_zero(key, sizeof(*key));
    code = pgvn_open_parent(path, &parent, name, sizeof(name));
    if (code) return code;
    file = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    close(parent);
    if (file < 0) return errno == ENOENT ? PGVN_PLATFORM_NOT_FOUND : errno == ELOOP ? PGVN_PLATFORM_DENIED : PGVN_PLATFORM_IO;
    if (fstat(file, &info) || !pgvn_key_stat_ok(&info, geteuid()) || info.st_size <= 0 || info.st_size >= (off_t)sizeof(text)) {
        close(file);
        return PGVN_PLATFORM_DENIED;
    }
    got = read(file, text, sizeof(text) - 1);
    close(file);
    if (got != info.st_size) {
        pgvn_secure_zero(text, sizeof(text));
        return PGVN_PLATFORM_IO;
    }
    text[got] = 0;
    code = PGVN_PLATFORM_DENIED;
    if ((size_t)got == strlen(text) && !memcmp(text, "PGVNKEY1\n", 9)) {
        id = text + 9;
        newline = strchr(id, '\n');
        if (newline) {
            *newline = 0;
            encoded = newline + 1;
            if (valid_id(id) && strlen(encoded) == 65 && encoded[64] == '\n' && !memchr(text, '\r', (size_t)got)) {
                encoded[64] = 0;
                if (hex_decode(key->secret, encoded, PGVN_AES_KEY)) {
                    memcpy(key->id, id, strlen(id) + 1);
                    code = PGVN_PLATFORM_OK;
                }
            }
        }
    }
    pgvn_secure_zero(text, sizeof(text));
    if (code) pgvn_secure_zero(key, sizeof(*key));
    return code;
}
