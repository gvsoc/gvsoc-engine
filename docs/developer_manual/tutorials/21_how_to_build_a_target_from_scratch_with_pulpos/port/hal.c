// SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
//
// SPDX-License-Identifier: Apache-2.0
//
// Authors: Germain Haugou (germain.haugou@gmail.com)

#include <stdio.h>
#include <stdint.h>
#include <kernel/hal.h>

static char __pi_libc_buffer[PI_LIBC_PUTC_BUFFER_SIZE];
static int __pi_libc_buffer_index = 0;

// Called by the kernel at boot, to initialize the SoC: nothing to do on this target
void __pi_init_soc()
{
}

// Called by printf for each character: the line is sent when complete or when the buffer is full
int __pi_libc_fputc_safe(int c, FILE *stream)
{
    __pi_libc_buffer[__pi_libc_buffer_index++] = c;

    if (__pi_libc_buffer_index == PI_LIBC_PUTC_BUFFER_SIZE || c == '\n')
    {
        __pi_libc_write(1, (uint8_t *)__pi_libc_buffer, __pi_libc_buffer_index);
        __pi_libc_buffer_index = 0;
    }

    return 0;
}
