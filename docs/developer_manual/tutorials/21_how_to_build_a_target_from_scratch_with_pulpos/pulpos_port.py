# SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

"""PulpOS port of my_target.

The executable of an application (see config.py) is an instance of MyTargetExecutable, which
gives the toolchain and contains the PulpOS module: the generic PulpOS kernel plus the code of
this port, in port/.
"""

import os
from typing import Any

import pulpos
import gvrun.target
from pulpos.toolchain import RiscvGccToolchain, ToolchainConfig


# Directory of the code of this port
PORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'port')


class MyTargetPulposModule(pulpos.PulposModule):

    def __init__(self, target: gvrun.target.SystemTreeNode, container: pulpos.SourceContainer):
        super().__init__(target, parent=container)

        # The generic kernel includes the port HAL with
        # <arch/$(CONFIG_CHIP_FAMILY_NAME)/kernel/hal.h>, found in port/
        self.add_define('CONFIG_CHIP_NAME', 'my_target')
        self.add_define('CONFIG_CHIP_FAMILY_NAME', 'my_target')
        # The kernel takes the IRQ functions (critical sections) from this header
        self.add_define('CONFIG_IRQ_INC', '<arch/my_target/kernel/irq.h>')
        self.add_includes(PORT_DIR)
        # The sources and the linker script of the port are given relative to port/, their
        # objects then go to the build directory
        self.add_source_path(PORT_DIR)

        # The configuration of the GVSoC target gives the memory map
        config = target.get_attributes()

        # The linker script is generated from a template, with the memory of the target
        linker_script = self.new_template_file('linker_script', 'link.ld', 'link.ld')
        linker_script.add_parameter('mem_start', f'0x{config.soc.mem_base:x}')
        linker_script.add_parameter('mem_size', f'0x{config.soc.mem_size:x}')
        # Code and data share the memory, hence a segment both writable and executable
        self.add_ldflags([f'-T{linker_script.get_path()}', '-Wl,--no-warn-rwx-segments'])

        # Same ISA as the core of the target
        march = f'{config.soc.isa}_zicsr'
        self.add_define('__RV32__', '1')
        self.add_cflags([f'-march={march}', '-mabi=ilp32'])
        self.add_ldflags([f'-march={march}', '-mabi=ilp32'])

        self.add_sources(['hal.c'])

        # The generic PulpOS kernel and libraries
        self.add_subdirectory(pulpos.get_home(self), target)


class MyTargetExecutable(pulpos.PulposExecutable):

    def __init__(self, name: str, target: gvrun.target.SystemTreeNode,
            parameters: list[tuple[str, Any]] | None=None):

        # Single core without interrupt controller: no thread scheduling and no events
        port_parameters: list[tuple[str, Any]] = [
            ('pulpos/kernel.threading', False),
            ('pulpos/kernel.event', False),
        ]
        if parameters is not None:
            port_parameters += parameters

        super().__init__(name, target, parameters=port_parameters)

        self.set_toolchain(RiscvGccToolchain(ToolchainConfig(path_from_env='RISCV32_GCC_TOOLCHAIN')))

        self.pulpos = MyTargetPulposModule(target, container=self)
