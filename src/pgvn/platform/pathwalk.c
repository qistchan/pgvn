#include "pgvn_platform.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

int pgvn_open_parent(const char *path, int *parent, char *name, size_t name_cap)
{
    int current, saved;
    size_t length, start;
    if (parent) *parent = -1;
    if (!path || path[0] != '/' || !parent || !name || name_cap < 2) return PGVN_PLATFORM_INVALID;
    length = strlen(path);
    if (length < 2 || length > 4096 || path[length - 1] == '/' || strchr(path, '\\')) return PGVN_PLATFORM_INVALID;
    current = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (current < 0) return PGVN_PLATFORM_IO;
    start = 1;
    while (start < length) {
        size_t end = start;
        while (end < length && path[end] != '/') ++end;
        if (end == start || end - start > 255 || (end - start == 1 && path[start] == '.')
            || (end - start == 2 && path[start] == '.' && path[start + 1] == '.')) {
            close(current);
            return PGVN_PLATFORM_INVALID;
        }
        if (end == length) {
            if (end - start + 1 > name_cap) { close(current); return PGVN_PLATFORM_INVALID; }
            memcpy(name, path + start, end - start);
            name[end - start] = 0;
            *parent = current;
            return PGVN_PLATFORM_OK;
        }
        char component[256];
        memcpy(component, path + start, end - start);
        component[end - start] = 0;
        saved = current;
        current = openat(saved, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(saved);
        if (current < 0) return errno == ELOOP || errno == EACCES ? PGVN_PLATFORM_DENIED : errno == ENOENT ? PGVN_PLATFORM_NOT_FOUND : PGVN_PLATFORM_IO;
        start = end + 1;
    }
    close(current);
    return PGVN_PLATFORM_INVALID;
}
