#pragma once
typedef unsigned long hash_t;
hash_t hashString(const char *str);
hash_t hashStringL(const char *str, int length);
hash_t hashStringS(const char *str, hash_t seed);
