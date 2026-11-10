#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

enum { ELEMENTS = 4096 };
static uint32_t input[ELEMENTS], output[ELEMENTS];

/* Keep each call observable to the caller so repeated vector work is retained. */
__attribute__((noipa))
static void add(uint32_t *restrict dst, const uint32_t *restrict src,
                uint32_t increment)
{
    for (size_t i = 0; i < ELEMENTS; ++i)
        dst[i] = src[i] + increment;
}

int main(int argc, char **argv)
{
    char *end;
    if (argc != 2)
        return 2;
    unsigned long rounds = strtoul(argv[1], &end, 10);
    if (*end || rounds < 1 || rounds > 1000000)
        return 2;

    for (size_t i = 0; i < ELEMENTS; ++i)
        input[i] = i;
    for (uint32_t round = 0; round < rounds; ++round)
        add(output, input, round);
    for (size_t i = 0; i < ELEMENTS; ++i)
        if (output[i] != i + rounds - 1)
            return 1;

    puts("rvv-add: ok");
    return 0;
}
