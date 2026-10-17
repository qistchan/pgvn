#ifndef PGVN_BUILD_INFO_H
#define PGVN_BUILD_INFO_H

#include <stddef.h>
#include "pgvn_build_info.h"

/* No credentials, repository names or local paths are exposed in build info. */
int pgvn_build_json(char *buffer, size_t size);

#endif
