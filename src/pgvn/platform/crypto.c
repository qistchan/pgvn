#include "pgvn_platform.h"
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <openssl/evp.h>
#include <openssl/rand.h>

uint64_t pgvn_monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u;
}

unsigned long pgvn_process_id(void)
{
    return (unsigned long)getpid();
}

void pgvn_secure_zero(void *data, size_t size)
{
    if (data && size) explicit_bzero(data, size);
}

int pgvn_random_bytes(void *data, size_t size)
{
    if (!data || !size || size > 4096) return PGVN_PLATFORM_INVALID;
    return RAND_bytes(data, (int)size) == 1 ? PGVN_PLATFORM_OK : PGVN_PLATFORM_CRYPTO;
}

int pgvn_sha256(unsigned char out[32], const void *data, size_t size)
{
    if (!out || (!data && size)) return PGVN_PLATFORM_INVALID;
    return EVP_Digest(data, size, out, NULL, EVP_sha256(), NULL) == 1 ? PGVN_PLATFORM_OK : PGVN_PLATFORM_CRYPTO;
}

int pgvn_utf8_valid(const void *data, size_t size)
{
    const unsigned char *bytes = data;
    size_t i = 0;
    if (!data && size) return 0;
    while (i < size) {
        unsigned char c = bytes[i];
        size_t need = 0;
        uint32_t point = 0;
        if (c <= 0x7f) { ++i; continue; }
        else if ((c & 0xe0) == 0xc0) { need = 2; point = c & 0x1f; if (c < 0xc2) return 0; }
        else if ((c & 0xf0) == 0xe0) { need = 3; point = c & 0x0f; }
        else if ((c & 0xf8) == 0xf0) { need = 4; point = c & 0x07; if (c > 0xf4) return 0; }
        else return 0;
        if (i + need > size) return 0;
        for (size_t n = 1; n < need; ++n) {
            if ((bytes[i + n] & 0xc0) != 0x80) return 0;
            point = (point << 6) | (bytes[i + n] & 0x3f);
        }
        if (need == 3 && point < 0x800) return 0;
        if (need == 4 && point < 0x10000) return 0;
        if (point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) return 0;
        i += need;
    }
    return 1;
}

int pgvn_identity_valid(const char *value, size_t max_bytes)
{
    size_t length, i;
    if (!value || !*value || max_bytes < 1) return 0;
    length = strlen(value);
    if (length > max_bytes || !pgvn_utf8_valid(value, length)) return 0;
    for (i = 0; i < length; ++i) {
        unsigned char c = (unsigned char)value[i];
        if (c < 32 || c == 127) return 0;
    }
    return 1;
}

static int gcm_bounds(const void *aad, size_t aad_len, const void *input, size_t input_len, unsigned char *output)
{
    if (aad_len > 1024 || input_len > 4096) return 0;
    if ((!aad && aad_len) || (!input && input_len) || !output) return 0;
    return 1;
}

int pgvn_aes256gcm_encrypt(const unsigned char key[PGVN_AES_KEY], const unsigned char nonce[PGVN_AES_NONCE],
    const void *aad, size_t aad_len, const void *plain, size_t plain_len,
    unsigned char *cipher, unsigned char tag[PGVN_AES_TAG])
{
    EVP_CIPHER_CTX *context;
    int length = 0, final_length = 0, ok;
    if (!key || !nonce || !tag || !gcm_bounds(aad, aad_len, plain, plain_len, cipher)) return PGVN_PLATFORM_INVALID;
    context = EVP_CIPHER_CTX_new();
    ok = context
        && EVP_EncryptInit_ex(context, EVP_aes_256_gcm(), NULL, NULL, NULL) == 1
        && EVP_CIPHER_CTX_ctrl(context, EVP_CTRL_GCM_SET_IVLEN, PGVN_AES_NONCE, NULL) == 1
        && EVP_EncryptInit_ex(context, NULL, NULL, key, nonce) == 1
        && EVP_EncryptUpdate(context, NULL, &length, aad, (int)aad_len) == 1
        && EVP_EncryptUpdate(context, cipher, &length, plain, (int)plain_len) == 1
        && length == (int)plain_len
        && EVP_EncryptFinal_ex(context, cipher + length, &final_length) == 1
        && final_length == 0
        && EVP_CIPHER_CTX_ctrl(context, EVP_CTRL_GCM_GET_TAG, PGVN_AES_TAG, tag) == 1;
    EVP_CIPHER_CTX_free(context);
    if (!ok) {
        if (plain_len) pgvn_secure_zero(cipher, plain_len);
        pgvn_secure_zero(tag, PGVN_AES_TAG);
        return PGVN_PLATFORM_CRYPTO;
    }
    return PGVN_PLATFORM_OK;
}

int pgvn_aes256gcm_decrypt(const unsigned char key[PGVN_AES_KEY], const unsigned char nonce[PGVN_AES_NONCE],
    const void *aad, size_t aad_len, const unsigned char *cipher, size_t cipher_len,
    const unsigned char tag[PGVN_AES_TAG], unsigned char *plain)
{
    EVP_CIPHER_CTX *context;
    int length = 0, final_length = 0, ok;
    if (!key || !nonce || !tag || !gcm_bounds(aad, aad_len, cipher, cipher_len, plain)) return PGVN_PLATFORM_INVALID;
    context = EVP_CIPHER_CTX_new();
    ok = context
        && EVP_DecryptInit_ex(context, EVP_aes_256_gcm(), NULL, NULL, NULL) == 1
        && EVP_CIPHER_CTX_ctrl(context, EVP_CTRL_GCM_SET_IVLEN, PGVN_AES_NONCE, NULL) == 1
        && EVP_DecryptInit_ex(context, NULL, NULL, key, nonce) == 1
        && EVP_DecryptUpdate(context, NULL, &length, aad, (int)aad_len) == 1
        && EVP_DecryptUpdate(context, plain, &length, cipher, (int)cipher_len) == 1
        && length == (int)cipher_len
        && EVP_CIPHER_CTX_ctrl(context, EVP_CTRL_GCM_SET_TAG, PGVN_AES_TAG, (void *)tag) == 1
        && EVP_DecryptFinal_ex(context, plain + length, &final_length) == 1
        && final_length == 0;
    EVP_CIPHER_CTX_free(context);
    if (!ok) {
        if (cipher_len) pgvn_secure_zero(plain, cipher_len);
        return PGVN_PLATFORM_CRYPTO;
    }
    return PGVN_PLATFORM_OK;
}
