#pragma once
#include <stdio.h>
#ifdef DEBUG
#define LOG(...) fprintf(stderr, __VA_ARGS__)
#else
#define LOG
#endif

#define LOG_F(...) fprintf(stderr, __VA_ARGS__)
