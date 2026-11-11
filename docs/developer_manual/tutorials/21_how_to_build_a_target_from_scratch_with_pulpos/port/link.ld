/* SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Linker script of my_target: everything in the memory. @mem_start@ and @mem_size@ are
 * replaced by the memory of the target (pulpos_port.py).
 */

OUTPUT_ARCH(riscv)
ENTRY( _start )
MEMORY
{
  mem : ORIGIN = @mem_start@, LENGTH = @mem_size@
}

SECTIONS
{
  /* The loader starts the core at the ELF entry point, _start (crt0) */
  .text :
  {
    . = ALIGN(16);
    _stext = .;
    *(.text)
    *(.text.*)
    *(.gnu.linkonce.t.*)
    _etext  =  .;
    *(.lit)
    *(.shdata)
    . = ALIGN(16);
    _endtext = .;
    . = ALIGN(16);
  } > mem

  .init :
  {
    . = ALIGN(8);
    KEEP( *(.init) )
    . = ALIGN(8);
  } > mem

  .fini :
  {
    . = ALIGN(8);
    KEEP( *(.fini) )
    . = ALIGN(8);
  } > mem

  .preinit_array : {
    . = ALIGN(8);
    PROVIDE_HIDDEN (__preinit_array_start = .);
    KEEP (*(.preinit_array))
    PROVIDE_HIDDEN (__preinit_array_end = .);
    . = ALIGN(8);
  } > mem

  .init_array : {
    . = ALIGN(8);
    PROVIDE_HIDDEN (__init_array_start = .);
    __CTOR_LIST__ = .;
    LONG((__CTOR_END__ - __CTOR_LIST__) / 4 - 2)
    KEEP(*(.ctors.start))
    KEEP(*(.ctors))
    KEEP (*(SORT(.init_array.*)))
    KEEP (*(.init_array ))
    LONG(0)
    __CTOR_END__ = .;
    PROVIDE_HIDDEN (__init_array_end = .);
    . = ALIGN(8);
  } > mem

  .fini_array : {
    . = ALIGN(8);
    PROVIDE_HIDDEN (__fini_array_start = .);
    __DTOR_LIST__ = .;
    LONG((__DTOR_END__ - __DTOR_LIST__) / 4 - 2)
    KEEP(*(.dtors.start))
    KEEP(*(.dtors))
    LONG(0)
    __DTOR_END__ = .;
    KEEP (*(SORT(.fini_array.*)))
    KEEP (*(.fini_array ))
    PROVIDE_HIDDEN (__fini_array_end = .);
    . = ALIGN(8);
  } > mem

  .boot : {
    . = ALIGN(8);
    *(.boot)
    *(.boot.data)
    . = ALIGN(8);
  } > mem

  .rodata : {
    . = ALIGN(8);
    *(.rodata);
    *(.rodata.*)
    *(.srodata);
    *(.srodata.*)
    *(.eh_frame*)
    *(.gnu.linkonce.r.*)
    . = ALIGN(8);
  } > mem

  .got : {
    . = ALIGN(8);
    *(.got.plt) * (.igot.plt) *(.got) *(.igot)
    . = ALIGN(8);
  } > mem

  .shbss : {
    . = ALIGN(8);
    *(.shbss)
    . = ALIGN(8);
  } > mem

  .talias : {
  } > mem

  .gnu.offload_funcs : {
    . = ALIGN(8);
    KEEP(*(.gnu.offload_funcs))
    . = ALIGN(8);
  } > mem

  .gnu.offload_vars : {
    . = ALIGN(8);
    KEEP(*(.gnu.offload_vars))
    . = ALIGN(8);
  } > mem

  /* Stack of main */
  .stack : {
    . = ALIGN(16);
    stack_start = .;
    . = . + 0x800;
    stack = .;
    . = ALIGN(8);
  } > mem

  .data : {
    . = ALIGN(8);
    sdata  =  .;
    _sdata  =  .;
    *(.data);
    *(.data.*)
    *(.sdata);
    *(.sdata.*)
    . = ALIGN(8);
    edata  =  .;
    _edata  =  .;
  } > mem

  .bss : {
    . = ALIGN(8);
    _bss_start = .;
    *(.bss)
    *(.bss.*)
    *(.sbss)
    *(.sbss.*)
    *(COMMON)
    . = ALIGN(8);
    _bss_end = .;
  } > mem

  .vectors ALIGN(256) :
  {
    __pi_irq_vector_base = .;
    KEEP(*(.vectors))
    . = . + 0x80;
    __pi_fast_irq_vector_base = .;
    KEEP(*(.fast_vectors))
    . = . + 0x80;
    . = ALIGN(8);
  } > mem
}
