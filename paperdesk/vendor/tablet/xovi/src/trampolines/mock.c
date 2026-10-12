#include "trampolines.h"
#include <stdio.h>

void generateUntrampoline(void *function, struct SymbolData *symbol, int bytesRemaining) {
    printf("[I]: Mock!: Generating untrampoline for symbol at %p into %p. There are %d bytes remaining\n", symbol, function, bytesRemaining);
}

struct SymbolData *pivotSymbol(const char *symbol, void *newaddr) {
    printf("[I]: Mock!: Generating trampoline for symbol %s to jump to %p\n", symbol, newaddr);
    return (void*) 0xFFFFFFFF;
}

