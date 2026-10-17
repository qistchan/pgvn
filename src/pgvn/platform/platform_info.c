#include "pgvn_platform.h"
#include <openssl/crypto.h>
#include <stdio.h>

int main(void)
{
    printf("{\"product\":\"PgVN\",\"version\":\"0.1.0-dev\",\"milestone\":\"M5\","
           "\"os\":\"linux\",\"arch\":\"x86_64\",\"svn_base\":\"1.14.5\","
           "\"credential_aead\":\"aes-256-gcm\",\"hook_executor\":\"posix\","
           "\"monitor\":\"evhttp\",\"svn_protocol\":false,\"dav_protocol\":false,"
           "\"process_id\":%lu,\"openssl\":\"%s\"}\n",
           pgvn_process_id(), OpenSSL_version(OPENSSL_VERSION));
    return 0;
}
