#include "hash.h"
#include <stdio.h>

hash_t hashString(const char *str)
{
    unsigned long hash = 5381;
    int c;

    while (c = *str++)
        hash = ((hash << 5) + hash) + c; /* hash * 33 + c */

    return hash;
}

hash_t hashStringS(const char *str, hash_t seed)
{
    unsigned long hash = seed;
    int c;

    while (c = *str++)
        hash = ((hash << 5) + hash) + c; /* hash * 33 + c */

    return hash;
}

hash_t hashStringL(const char *str, int length)
{
    unsigned long hash = 5381;
    int c;

    while (length && (c = *str++)) {
        hash = ((hash << 5) + hash) + c; /* hash * 33 + c */
        --length;
    }

    return hash;
}
