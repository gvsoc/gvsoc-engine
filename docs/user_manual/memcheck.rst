Memory Checker
--------------

Introduction
++++++++++++

The memory checker, referred to as memcheck, is a feature that detects invalid accesses made by
the simulated software.

It currently detects the following kinds of invalid accesses:

- Uninitialized accesses
- Buffer underflows and overflows
- Use-after-free
- Cross-region accesses (an access landing in another memory than the one the buffer was
  allocated from)

All reports use the real physical addresses of the simulated system, so they can directly be
correlated with linker maps, traces, and debugger output.

Usage
+++++

Memcheck can be enabled by adding the *--memcheck* option: ::

    gvrun --target gap.gap9.evk --parameter binary=test build run --memcheck

On targets running PulpOS, the allocator instrumentation must also be compiled in by setting the
*pulpos/kernel.memcheck* parameter to true (see `Runtime Support`_ below).

Once a fault is detected, a warning is dumped by GVSOC: ::

  515864014: 19271: [/chip/soc/fc/core/regfile] Conditional jump depends on uninitialised register (reg: 14)
  Input error: Platform returned an error (exitcode: 1)

By default, GVSOC exits when such a warning is raised. To receive the warning but prevent GVSOC
from exiting, add the *--no-werror* option. The faulty access itself always proceeds normally, so
the program behavior does not depend on memcheck being enabled.

Possible errors are explained in the following sections.

Buffer Tracking
+++++++++++++++

Behavior
........

Each dynamic allocation is registered to a global registry through a semi-hosting call and gets a
unique buffer ID. The ID is attached to the register holding the pointer returned by the
allocator, and the core model propagates it alongside the pointer:

- Register moves, pointer arithmetic (add/sub with an offset) and alignment masking keep the ID.
- Storing the pointer to memory records the ID in a shadow location, and loading it back restores
  it, so pointers spilled to the stack or kept in data structures keep naming their buffer. This
  also works across cores, for instance when a pointer is passed to a cluster core through a
  shared structure.
- Operations that do not produce a pointer (constants, shifts, multiplications, the difference of
  two pointers to the same buffer) drop the ID.

Every load and store is then checked against the bounds of the buffer its address derives from,
by the core model at issue time. The address is first folded to its canonical global form using
the same alias windows the platform declares for watchpoints, so buffers accessed through a
master-local alias are checked correctly. An access landing in a different declared region than
the buffer's one is reported as a cross-region access naming both.

Freeing a buffer marks it as freed in the registry but its ID is never reused, so a stale pointer
kept by the application keeps naming the freed buffer and any access through it is reported as
use-after-free, even after the allocator has recycled the memory.

Since this mechanism is based on dynamic allocations, no fault can be detected on static variables
or on the stack. Any faulty access to a buffer declared as a global variable will not be detected.

Coverage relies on the ID following the pointer. It is dropped when the pointer takes a path the
core model cannot track, in which case the access is silently not checked (no false positives):

- Pointers reconstructed through integer arithmetic that does not qualify as pointer arithmetic
  (XOR tricks, shifts, manual bit packing).
- Sub-word or misaligned pointer stores (the shadow tracks pointer-sized aligned slots).
- Accesses done by DMA engines or hardware accelerators.
- Pointer values stored to memory across paths that rebuild requests (e.g. the SoC-cluster
  bridge adapters), which drop the shadow sideband.

Faults
......

An overflow can be triggered with the following code:

.. code-block:: C

    char *buff = pi_malloc(1024);
    buff[1024] = 0;

It is reported like this:

.. code-block:: shell

    280019297: 23591: [/chip/soc/fc/core/lsu] Invalid write of 1 bytes at 0x1c001678, 0 bytes after buffer #1 (region: l2_priv, base: 0x1c001638, size: 0x400, allocated at pc 0x1c010a10 ra 0x1c0109fc)
    Input error: Platform returned an error (exitcode: 1)

The report names the faulting core and gives the real address of the access, the distance to the
buffer, the buffer identity and bounds, and the program counter and return address of the
allocation call site.

A use-after-free is reported like this:

.. code-block:: shell

    285768075: 25703: [/chip/soc/fc/core/lsu] Invalid read of 1 bytes at 0x1c001638 through a pointer to freed buffer #1 (region: l2_priv, base: 0x1c001638, size: 0x40, allocated at pc 0x1c010a10 ra 0x1c0109fc, freed at pc 0x1c010a4a ra 0x1c010ae2)

And an access landing in another region like this:

.. code-block:: shell

    321594355: 46229: [/chip/soc/fc/core/lsu] Invalid read of 1 bytes at 0x1c012000 in region l2_shared through a pointer to another region's buffer #1 (region: l1, base: 0x10000000, size: 0x20, allocated at pc 0x1c0104c0 ra 0x1c0104a4)

Uninitialized Accesses
+++++++++++++++++++++++

Behavior
........

When memcheck is enabled, memory models instantiate a dedicated valid array next to the classic
data array, with the same size. This valid array is initialized so that any data bit is considered
uninitialized. There is one valid bit for each data bit, so the granularity of the check is a bit.

Any write access to the memory model turns the corresponding valid bits into initialized bits.

Any read access to the memory model reports the valid bits to the initiator, which takes them into
account.

The whole memory is set as uninitialized when the simulation starts.

Currently, only the core model checks them. It allows read accesses to uninitialized locations but
raises a fault if an uninitialized value is used for:

- A branch, since it can lead to a jump to an invalid address
- A memory access, since it leads to an invalid address

Faults
......

Many uninitialized accesses are not reported because it is legal to load an uninitialized location.
The compiler can do this for speculation. For example, it can load a value in advance while not
being sure it is valid because it depends on the result of a check.

The most common fault for uninitialized accesses is loading a value from an uninitialized location
and using it for a check. This fault can make the core randomly jump to a branch or another.

This can be triggered with the following code:

.. code-block:: C

    char *buff = pi_malloc(1024);
    if (buff[256])
    {
        exit(0);
    }

This will report the following warning:

.. code-block:: shell

    559544241: 36866: [/chip/soc/fc/core/regfile] Conditional jump depends on uninitialised register (reg: 14)
    Input error: Platform returned an error (exitcode: 1)

The second kind of error occurs when the core tries to use an uninitialized value to build an
address and accesses it. It is reported as:

.. code-block:: shell

    511737517: 34945: [/chip/soc/fc/core/regfile] Access address depends on uninitialised register (reg: 14)
    Input error: Platform returned an error (exitcode: 1)

Runtime Support
+++++++++++++++

Memory allocators running on the target must declare their allocations using two semi-hosting
calls, accessible through the gvsoc target header (gvsoc.h):

.. code-block:: C

    static inline void *gv_memcheck_mem_alloc(int mem_id, void *ptr, size_t size);

    static inline void *gv_memcheck_mem_free(int mem_id, void *ptr, size_t size);

The first argument is the memory region identifier where the operation is performed. Each
allocator-backed memory region of the simulated system is assigned an identifier on the platform
side (see `Platform Support`_), and the same value must be passed here.

*gv_memcheck_mem_alloc* registers an allocated chunk and returns the pointer with its buffer ID
attached, so its return value is what the allocator must hand back to the caller.
*gv_memcheck_mem_free* unregisters the chunk and returns the pointer with its ID stripped, so
that the allocator's own metadata accesses to the recycled chunk stay silent while stale
application copies keep triggering use-after-free reports. Both calls return the pointer
unchanged in value.

On PulpOS, the standard allocator (pi_mem_alloc / pi_mem_free and their L1/L2 variants) is
already instrumented. The instrumentation is compiled in by setting the *pulpos/kernel.memcheck*
build parameter to true, and is only active on the gvsoc platform.

Platform Support
++++++++++++++++

On the platform side, a target wires memcheck by declaring its allocator-backed regions with the
*utils.memcheck_regions* declaration component (a pure declaration carrier, like the watchpoint
alias one): one entry per region with its ID, name, global base and size. Nothing in the request
path needs any memcheck configuration; the memories only maintain the generic shadow storage,
whatever the bank topology.

The buffer checks run in the core models with the address the program computed. If a master
reaches a memory through a local alias, declare the alias window with
*utils.watchpoint_alias* (needed for watchpoints anyway): the cores fold accessed addresses
through the same table before checking.

GDB Support
+++++++++++

The memcheck warnings provide information about the invalid access but not about the code that
triggered the fault.

To get more information, GDB can be connected. Any memcheck register fault triggers a bus error,
which is caught by GDB. GDB then shows the source code where the bus error occurred and gives
control back to the user.

Once GDB is connected, an uninitialized-register fault makes GDB print the following message:

.. code-block:: shell

    Thread 11 received signal SIGBUS, Bus error.
    0x1c010376 in main (argc=<optimized out>, argv=<optimized out>) at test.c:8
    8        buff[1024] = 10;
    (gdb)

From there, the backtrace can be shown, and variables can be dumped to understand what led to this
fault.
