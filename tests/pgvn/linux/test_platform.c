#include "pgvn_platform.h"
#include "chunk.h"
#include "hook_name.h"
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int failures;

#define CHECK(condition) do { \
    if (!(condition)) { fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #condition); failures++; } \
} while (0)

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

static int write_script(const char *path, const char *body)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0700);
    size_t length, done = 0;
    if (fd < 0) return -1;
    length = strlen(body);
    while (done < length) {
        ssize_t wrote = write(fd, body + done, length - done);
        if (wrote <= 0) { close(fd); return -1; }
        done += (size_t)wrote;
    }
    return close(fd);
}

static void test_crypto(void)
{
    static const unsigned char key[32] = {
        0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    };
    static const unsigned char nonce[12] = {0,1,2,3,4,5,6,7,8,9,10,11};
    static const unsigned char expect_ct[] = {0x34,0x31,0xb5,0x69,0xa0,0x91,0xef,0xfe,0x22,0xc7,0x70,0x2b,0x30};
    static const unsigned char expect_tag[] = {
        0x8a,0xbf,0xf3,0x43,0xda,0x73,0xe0,0x69,0x2c,0x9e,0x37,0x02,0x60,0x07,0x71,0x3b
    };
    const char *aad = "pgvn\nalice\ndeployment-v1";
    const char *plain = "s3cret-密码";
    unsigned char cipher[32], tag[16], opened[32], bad[16];
    CHECK(pgvn_utf8_valid(plain, strlen(plain)));
    CHECK(!pgvn_utf8_valid("\xc0\x80", 2));
    CHECK(!pgvn_utf8_valid("\xed\xa0\x80", 3));
    CHECK(pgvn_identity_valid("alice", 128));
    CHECK(pgvn_identity_valid("文件", 128));
    CHECK(!pgvn_identity_valid("bad\nname", 128));
    CHECK(!pgvn_identity_valid("bad\rname", 128));
    CHECK(!pgvn_aes256gcm_encrypt(key, nonce, aad, strlen(aad), plain, strlen(plain), cipher, tag));
    CHECK(!memcmp(cipher, expect_ct, sizeof(expect_ct)));
    CHECK(!memcmp(tag, expect_tag, sizeof(expect_tag)));
    CHECK(!pgvn_aes256gcm_decrypt(key, nonce, aad, strlen(aad), cipher, strlen(plain), tag, opened));
    CHECK(!memcmp(opened, plain, strlen(plain)));
    memcpy(bad, tag, sizeof(bad));
    bad[0] ^= 1;
    CHECK(pgvn_aes256gcm_decrypt(key, nonce, aad, strlen(aad), cipher, strlen(plain), bad, opened) == PGVN_PLATFORM_CRYPTO);
    CHECK(pgvn_aes256gcm_decrypt(key, nonce, "pgvn\nbob\ndeployment-v1", 22, cipher, strlen(plain), tag, opened) == PGVN_PLATFORM_CRYPTO);
}

static void test_keys(const char *root)
{
    char path[512], linkpath[512], loose[512];
    pgvn_loaded_key key, again;
    struct stat info;
    int fd;
    const char *crlf = "PGVNKEY1\r\nabc\r\n";
    snprintf(path, sizeof(path), "%s/credentials.key", root);
    CHECK(pgvn_key_create("relative.key", "deployment-v1") == PGVN_PLATFORM_INVALID);
    CHECK(!pgvn_key_create(path, "deployment-v1"));
    CHECK(pgvn_key_create(path, "deployment-v1") == PGVN_PLATFORM_EXISTS);
    CHECK(!pgvn_key_load(&key, path));
    CHECK(!strcmp(key.id, "deployment-v1"));
    CHECK(!pgvn_key_load(&again, path));
    CHECK(!memcmp(key.secret, again.secret, 32));
    snprintf(linkpath, sizeof(linkpath), "%s/credentials.link", root);
    CHECK(!symlink(path, linkpath));
    CHECK(pgvn_key_load(&again, linkpath) == PGVN_PLATFORM_DENIED);
    CHECK(!chmod(path, 0644));
    CHECK(pgvn_key_load(&again, path) == PGVN_PLATFORM_DENIED);
    CHECK(!chmod(path, 0600));
    CHECK(!pgvn_key_load(&again, path));
    snprintf(loose, sizeof(loose), "%s/crlf.key", root);
    fd = open(loose, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    CHECK(fd >= 0);
    CHECK(write(fd, crlf, strlen(crlf)) == (ssize_t)strlen(crlf));
    close(fd);
    CHECK(pgvn_key_load(&again, loose) == PGVN_PLATFORM_DENIED);
    memset(&info, 0, sizeof(info));
    info.st_mode = S_IFREG | 0640;
    info.st_uid = geteuid();
    info.st_nlink = 1;
    CHECK(!pgvn_key_stat_ok(&info, geteuid()));
    info.st_mode = S_IFREG | 0600;
    info.st_nlink = 2;
    CHECK(!pgvn_key_stat_ok(&info, geteuid()));
    info.st_nlink = 1;
    CHECK(pgvn_key_stat_ok(&info, geteuid()));
    pgvn_secure_zero(&key, sizeof(key));
    pgvn_secure_zero(&again, sizeof(again));
}

static void test_names(void)
{
    char out[64];
    const char *map = "PGVNHOOKMAP1\npre-commit.exe check_commit\n# comment\n";
    CHECK(pgvn_hook_executable_valid("pre-commit.exe", 1));
    CHECK(pgvn_hook_executable_valid("pre-commit.EXE", 1));
    CHECK(!pgvn_hook_executable_valid("pre-commit", 1));
    CHECK(!pgvn_hook_executable_valid("pre-commit.sh", 1));
    CHECK(!pgvn_hook_executable_valid("../pre-commit.exe", 1));
    CHECK(pgvn_hook_executable_valid("pre-commit", 0));
    CHECK(pgvn_hook_executable_valid("pre-commit.exe", 0));
    CHECK(!pgvn_hook_executable_valid("pre-commit.sh", 0));
    CHECK(!pgvn_hook_executable_valid("-pre", 0));
    CHECK(pgvn_hook_map_resolve("pre-commit.exe", NULL, out, sizeof(out)));
    CHECK(!strcmp(out, "pre-commit"));
    CHECK(pgvn_hook_map_resolve("pre-commit.exe", map, out, sizeof(out)));
    CHECK(!strcmp(out, "check_commit"));
    CHECK(!pgvn_hook_map_resolve("missing.exe", map, out, sizeof(out)));
}

static void test_lru_and_chunk(void)
{
    pgvn_lru *cache = pgvn_lru_create(8, 2, 1, 9);
    char out[8];
    size_t length = sizeof(out);
    CHECK(cache);
    CHECK(pgvn_lru_put(cache, "a", 1, "12345", 5, 0) == PGVN_PLATFORM_DENIED);
    CHECK(!pgvn_lru_put(cache, "a", 1, "12345", 5, 1));
    CHECK(!pgvn_lru_put(cache, "b", 1, "67890", 5, 1));
    CHECK(pgvn_lru_get(cache, "a", 1, out, &length) == PGVN_PLATFORM_NOT_FOUND);
    length = sizeof(out);
    CHECK(!pgvn_lru_get(cache, "b", 1, out, &length));
    CHECK(length == 5 && !memcmp(out, "67890", 5));
    pgvn_lru_test_corrupt(cache, "b", 1);
    length = sizeof(out);
    CHECK(pgvn_lru_get(cache, "b", 1, out, &length) == PGVN_PLATFORM_NOT_FOUND);
    CHECK(!pgvn_lru_put(cache, "c", 1, "hello", 5, 1));
    pgvn_lru_set_epoch(cache, 10);
    length = sizeof(out);
    CHECK(pgvn_lru_get(cache, "c", 1, out, &length) == PGVN_PLATFORM_NOT_FOUND);
    pgvn_lru_destroy(cache);
    CHECK(pgvn_chunk_length_ok(0, 1, 0, 1048576));
    CHECK(pgvn_chunk_length_ok(0, 1, 1048576, 1048576));
    CHECK(!pgvn_chunk_length_ok(1, 1, 0, 1048576));
    CHECK(!pgvn_chunk_length_ok(0, 0, 4, 1048576));
    CHECK(!pgvn_chunk_length_ok(0, 1, 1048577, 1048576));
}

static void test_dispatch(void)
{
    pgvn_http_input input;
    pgvn_http_decision decision;
    memset(&input, 0, sizeof(input));
    input.method_is_get = 1;
    input.path_valid = 1;
    input.path = "/health/live";
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 200 && decision.route == PGVN_HTTP_LIVE);
    input.headers_too_large = 1;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 431 && !strcmp(decision.error_code, "headers_too_large"));
    input.headers_too_large = 0;
    input.method_is_get = 0;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 405);
    input.method_is_get = 1;
    input.has_body = 1;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 413);
    input.has_body = 0;
    input.path_valid = 0;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 400);
    input.path_valid = 1;
    input.path = "/missing";
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 404);
    input.path = "/metrics";
    input.origin_count = 1;
    input.origin = "http://evil";
    input.expected_origin = "http://127.0.0.1:14691";
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 403);
    input.origin = input.expected_origin;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 401);
    input.authorized = 1;
    input.query_present = 1;
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 400 && !strcmp(decision.error_code, "invalid_query"));
    input.query_present = 0;
    input.path = "/api/monitor/v1/status";
    pgvn_http_decide(&input, &decision);
    CHECK(decision.status == 200 && decision.route == PGVN_HTTP_API);
}

static void test_tokens(void)
{
    pgvn_token_store *store = pgvn_token_store_create();
    unsigned char digest[32];
    char hex[65], header[80];
    const char *secret = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    snprintf(header, sizeof(header), "Bearer pgvn_%s", secret);
    CHECK(!pgvn_sha256(digest, header + 7, 69));
    hex_encode(hex, digest, 32);
    CHECK(!pgvn_token_store_add(store, hex, 60));
    CHECK(pgvn_token_authorize(store, header, strlen(header)));
    CHECK(!pgvn_token_authorize(store, "Bearer pgvn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 76));
    pgvn_token_store_destroy(store);
    store = pgvn_token_store_create();
    CHECK(!pgvn_token_store_add(store, hex, 0));
    CHECK(!pgvn_token_authorize(store, header, strlen(header)));
    pgvn_token_store_destroy(store);
}

struct cancel_wait { volatile sig_atomic_t *flag; };

static void *raise_cancel(void *argument)
{
    struct cancel_wait *wait = argument;
    usleep(200000);
    *wait->flag = 1;
    return NULL;
}

static void test_spawn(const char *root)
{
    char show[512], slow[512], flood[512], escape[512], linkpath[512], pid_path[512];
    const char *show_args[] = {"$(id)", NULL};
    const char *utf8_args[] = {"文件-café", NULL};
    const char *pid_args[] = {NULL, NULL};
    pgvn_spawn_request request;
    pgvn_spawn_result result;
    const char *names[] = {"show"};
    volatile sig_atomic_t cancel = 0;
    pthread_t thread;
    struct cancel_wait wait = {&cancel};
    int pid = 0;
    FILE *file;
    memset(&request, 0, sizeof(request));
    snprintf(show, sizeof(show), "%s/show", root);
    snprintf(slow, sizeof(slow), "%s/slow", root);
    snprintf(flood, sizeof(flood), "%s/flood", root);
    snprintf(escape, sizeof(escape), "%s/escape", root);
    snprintf(pid_path, sizeof(pid_path), "%s/pid", root);
    CHECK(!write_script(show, "#!/usr/bin/python3\nimport sys\nsys.stdout.buffer.write(sys.argv[1].encode())\n"));
    CHECK(!write_script(slow, "#!/usr/bin/python3\nimport os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(30)\n"));
    CHECK(!write_script(flood, "#!/usr/bin/python3\nimport sys\nsys.stdout.write('A'*20000)\nsys.stdout.flush()\n"));
    CHECK(!write_script(escape, "#!/usr/bin/python3\nimport os,sys,time\npid=os.fork()\n"
        "if pid==0:\n os.setsid()\n open(sys.argv[1],'w').write(str(os.getpid()))\n time.sleep(30)\n"
        "else:\n os._exit(0)\n"));
    CHECK(!pgvn_hook_files_ready(root, names, 1));
    CHECK(pgvn_hook_files_ready(root, (const char *[]){"missing"}, 1) == PGVN_PLATFORM_NOT_FOUND);
    request.root = root;
    request.executable = "show";
    request.arguments = show_args;
    request.timeout_seconds = 5;
    request.output_limit = 8192;
    CHECK(!pgvn_spawn_capture(&request, &result));
    CHECK(result.code == PGVN_PLATFORM_OK && result.exit_code == 0);
    CHECK(!strcmp(result.stdout_buf, "$(id)"));
    request.arguments = utf8_args;
    CHECK(!pgvn_spawn_capture(&request, &result));
    CHECK(!strcmp(result.stdout_buf, "文件-café"));
    request.executable = "flood";
    request.arguments = NULL;
    CHECK(!pgvn_spawn_capture(&request, &result));
    CHECK(result.truncated_stdout && result.stdout_len == 8192);
    snprintf(linkpath, sizeof(linkpath), "%s/linked", root);
    CHECK(!symlink(show, linkpath));
    request.executable = "linked";
    CHECK(pgvn_spawn_capture(&request, &result) == PGVN_PLATFORM_DENIED);
    CHECK(!chmod(flood, 0644));
    request.executable = "flood";
    CHECK(pgvn_spawn_capture(&request, &result) == PGVN_PLATFORM_DENIED);
    pid_args[0] = pid_path;
    request.executable = "slow";
    request.arguments = pid_args;
    request.timeout_seconds = 1;
    CHECK(pgvn_spawn_capture(&request, &result) == PGVN_PLATFORM_TIMEOUT);
    file = fopen(pid_path, "r");
    CHECK(file && fscanf(file, "%d", &pid) == 1);
    if (file) fclose(file);
    CHECK(pid > 1 && kill(pid, 0) < 0);
    unlink(pid_path);
    cancel = 0;
    request.timeout_seconds = 30;
    request.cancel = &cancel;
    CHECK(!pthread_create(&thread, NULL, raise_cancel, &wait));
    CHECK(pgvn_spawn_capture(&request, &result) == PGVN_PLATFORM_CANCELLED);
    pthread_join(thread, NULL);
    request.cancel = NULL;
    unlink(pid_path);
    request.executable = "escape";
    request.timeout_seconds = 5;
    CHECK(pgvn_spawn_capture(&request, &result) == PGVN_PLATFORM_DESCENDANT);
    file = fopen(pid_path, "r");
    if (file) {
        CHECK(fscanf(file, "%d", &pid) == 1);
        fclose(file);
        CHECK(kill(pid, 0) < 0);
    }
}

int main(void)
{
    char root[] = "/tmp/pgvn-platform-XXXXXX";
    CHECK(mkdtemp(root));
    test_crypto();
    test_keys(root);
    test_names();
    test_lru_and_chunk();
    test_dispatch();
    test_tokens();
    test_spawn(root);
    if (failures) fprintf(stderr, "%d platform checks failed\n", failures);
    return failures ? 1 : 0;
}
