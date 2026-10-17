#ifndef PGVN_HOOK_NAME_H
#define PGVN_HOOK_NAME_H
#include <stddef.h>
/* require_exe selects the Windows basename rule (suffix .exe).
 * Linux accepts that legacy form and an extensionless logical name.
 * Neither form allows a path, a shell metacharacter, or a foreign extension. */
int pgvn_hook_executable_valid(const char *name, int require_exe);
/* map_text NULL uses the default: strip a trailing .exe, otherwise keep a
 * logical name. A PGVNHOOKMAP1 document overrides that and fails closed. */
int pgvn_hook_map_resolve(const char *configured, const char *map_text, char *out, size_t out_cap);
#endif
