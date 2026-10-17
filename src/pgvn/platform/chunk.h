#ifndef PGVN_CHUNK_H
#define PGVN_CHUNK_H
/* Bytea length from PQgetlength / PQgetisnull / PQfformat.
 * An empty chunk is length 0 and is not SQL NULL. A full block may be
 * exactly max_length (1 MiB). Anything else is not a content chunk. */
static inline int pgvn_chunk_length_ok(int is_null, int is_binary, int length, int max_length)
{
    if (is_null || !is_binary || max_length < 0 || length < 0 || length > max_length) return 0;
    return 1;
}
#endif
