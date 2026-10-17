#include "hook_name.h"
#include <string.h>

static int ends_with_exe(const char *name, size_t length)
{
    const unsigned char *suffix;
    if (length < 4) return 0;
    suffix = (const unsigned char *)name + length - 4;
    return suffix[0] == '.'
        && (suffix[1] == 'e' || suffix[1] == 'E')
        && (suffix[2] == 'x' || suffix[2] == 'X')
        && (suffix[3] == 'e' || suffix[3] == 'E');
}

static int windows_name(const char *name, size_t length)
{
    size_t i;
    if (length < 5 || length > 132 || !ends_with_exe(name, length)) return 0;
    for (i = 0; i < length; ++i) {
        unsigned char c = (unsigned char)name[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
            || c == '-' || c == '_' || c == '.')) return 0;
    }
    return 1;
}

static int logical_name(const char *name, size_t length)
{
    size_t i;
    unsigned char first;
    if (length < 1 || length > 128) return 0;
    first = (unsigned char)name[0];
    if (!((first >= 'A' && first <= 'Z') || (first >= 'a' && first <= 'z')
        || (first >= '0' && first <= '9') || first == '_')) return 0;
    for (i = 1; i < length; ++i) {
        unsigned char c = (unsigned char)name[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
            || c == '_' || c == '-')) return 0;
    }
    return 1;
}

int pgvn_hook_executable_valid(const char *name, int require_exe)
{
    size_t length;
    if (!name || !*name) return 0;
    length = strlen(name);
    if (require_exe) return windows_name(name, length);
    return windows_name(name, length) || logical_name(name, length);
}

static int copy_name(char *out, size_t cap, const char *value, size_t length)
{
    char probe[133];
    if (!out || !value || length >= sizeof(probe) || length + 1 > cap) return 0;
    memcpy(probe, value, length);
    probe[length] = 0;
    if (!pgvn_hook_executable_valid(probe, 0)) return 0;
    memcpy(out, probe, length + 1);
    return 1;
}

int pgvn_hook_map_resolve(const char *configured, const char *map_text, char *out, size_t out_cap)
{
    size_t length;
    const char *cursor;
    int found = 0;
    if (!pgvn_hook_executable_valid(configured, 0)) return 0;
    length = strlen(configured);
    if (!map_text) {
        if (ends_with_exe(configured, length)) {
            if (!logical_name(configured, length - 4)) return 0;
            return copy_name(out, out_cap, configured, length - 4);
        }
        return logical_name(configured, length) && copy_name(out, out_cap, configured, length);
    }
    if (strncmp(map_text, "PGVNHOOKMAP1\n", 13)) return 0;
    cursor = map_text + 13;
    while (*cursor) {
        const char *line = cursor, *break_at = strchr(cursor, '\n'), *space;
        size_t line_length, left_length, right_length;
        char left[133], right[133];
        if (!break_at) return 0;
        line_length = (size_t)(break_at - line);
        cursor = break_at + 1;
        if (!line_length || line[0] == '#') continue;
        space = memchr(line, ' ', line_length);
        if (!space || memchr(space + 1, ' ', (size_t)(break_at - space - 1))) return 0;
        left_length = (size_t)(space - line);
        right_length = (size_t)(break_at - space - 1);
        if (!left_length || !right_length || left_length >= sizeof(left) || right_length >= sizeof(right)) return 0;
        memcpy(left, line, left_length);
        left[left_length] = 0;
        memcpy(right, space + 1, right_length);
        right[right_length] = 0;
        if (!pgvn_hook_executable_valid(left, 0) || !pgvn_hook_executable_valid(right, 0)) return 0;
        if (left_length == length && !memcmp(left, configured, length)) {
            if (found || !copy_name(out, out_cap, right, right_length)) return 0;
            found = 1;
        }
    }
    return found;
}
