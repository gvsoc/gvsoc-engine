// SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
//
// SPDX-License-Identifier: Apache-2.0
//
// Authors: Germain Haugou (germain.haugou@gmail.com)

// IRQ functions of my_target, included by the generic kernel (pmsis/kernel/irq.h) through
// CONFIG_IRQ_INC

#pragma once

#include <kernel/riscv.h>

// Critical sections of the kernel: the interrupts of the core are masked with mstatus.MIE
static inline int pi_irq_lock()
{
    int state;
    __asm__ __volatile__ ("csrrc %0, %1, %2" : "=r" (state) : "I" (CSR_MSTATUS),
        "I" (1 << CSR_MSTATUS_MIE_BIT) : "memory");
    return state;
}

static inline void pi_irq_unlock(int state)
{
    __asm__ __volatile__ ("csrw %0, %1" : : "I" (CSR_MSTATUS), "r" (state) : "memory");
}
