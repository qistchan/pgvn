#ifndef PGVN_PLATFORM_INTERNAL_H
#define PGVN_PLATFORM_INTERNAL_H
#include "pgvn_platform.h"
int pgvn_open_parent(const char *path, int *parent, char *name, size_t name_cap);
#endif
