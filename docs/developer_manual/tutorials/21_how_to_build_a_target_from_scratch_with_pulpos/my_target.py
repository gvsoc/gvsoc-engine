# SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

"""A target with a single RISC-V core and a memory, on which PulpOS runs.

The PulpOS port of this target is in pulpos_port.py and port/.
"""

import gvsoc.systree
import gvsoc.runner
from config_tree import Config, cfg_field
from gvrun.parameter import TargetParameter
from vp.clock_domain import Clock_domain
from cpu.iss_v2.riscv import Riscv
from cpu.iss_v2.riscv_config import RiscvConfig
from memory.memory_v3 import Memory, MemoryV3Config
from interco.router_v2 import Router, RouterConfig, RouterMapping
from utils.loader.loader_v2 import ElfLoader


class MySocConfig(Config):
    """Configuration of the SoC: the memory map and the configurations of the components."""

    mem_base: int = cfg_field(default=0x8000_0000, fmt="hex", dump=True, desc=(
        "Base address of the memory"
    ))

    mem_size: int = cfg_field(default=0x10_0000, fmt="hex", dump=True, desc=(
        "Size of the memory"
    ))

    isa: str = cfg_field(default='rv32imc', dump=True, desc=(
        "ISA of the core"
    ))

    core: RiscvConfig = cfg_field(init=False, desc=(
        "Core configuration"
    ))

    mem: MemoryV3Config = cfg_field(init=False, desc=(
        "Memory configuration"
    ))

    ico: RouterConfig = cfg_field(init=False, desc=(
        "Interconnect configuration"
    ))

    mem_mapping: RouterMapping = cfg_field(init=False, desc=(
        "Address range of the memory on the interconnect"
    ))

    def __post_init__(self):
        super().__post_init__()
        # No HTIF, PulpOS prints and exits through semihosting
        self.core = RiscvConfig(isa=self.isa, htif=False)
        self.mem = MemoryV3Config('mem', size=self.mem_size, latency=1)
        self.ico = RouterConfig()
        self.mem_mapping = RouterMapping(name='mem', base=self.mem_base, size=self.mem_size)


class MyBoardConfig(Config):
    """Configuration of the board: the clock and the SoC."""

    frequency: int = cfg_field(default=100_000_000, dump=True, desc=(
        "Frequency in Hz of the SoC"
    ))

    soc: MySocConfig = cfg_field(init=False, desc=(
        "SoC configuration"
    ))

    def __post_init__(self):
        super().__post_init__()
        self.soc = MySocConfig('soc')


class MySoc(gvsoc.systree.Component):

    def __init__(self, parent: gvsoc.systree.Component, name: str, config: MySocConfig):
        super().__init__(parent, name, config=config)

        # The binary is given by gvrun, which builds it from config.py
        TargetParameter(self, name='binary', value=None,
            description='Binary to be loaded and started', cast=str)

        mem    = Memory   (self, 'mem'   , config=config.mem )
        ico    = Router   (self, 'ico'   , config=config.ico )
        core   = Riscv    (self, 'core'  , config=config.core)
        loader = ElfLoader(self, 'loader'                    )

        ico.o_MAP(mem.i_INPUT(), mapping=config.mem_mapping)

        # One interconnect input per master
        core.o_FETCH(ico.i_INPUT(0))
        core.o_DATA (ico.i_INPUT(1))

        # The loader writes the binary into the memory, then gives the entry point to the core
        # and starts it
        loader.o_OUT  (ico.i_INPUT(2) )
        loader.o_START(core.i_FETCHEN())
        loader.o_ENTRY(core.i_ENTRY() )

        self.loader = loader
        self.register_binary_handler(self.handle_binary)

    def configure(self) -> None:
        binary = self.get_parameter('binary')
        if binary is not None:
            self.loader.set_binary(binary)

    def handle_binary(self, binary: str):
        self.set_parameter('binary', binary)


class MyBoard(gvsoc.systree.Component):

    def __init__(self, parent: gvsoc.systree.Component, name: str, config: MyBoardConfig):
        super().__init__(parent, name, config=config)

        clock = Clock_domain(self, 'clock', frequency=config.frequency)
        soc   = MySoc       (self, 'soc'  , config=config.soc         )

        clock.o_CLOCK(soc.i_CLOCK())


# The target that gvrun instantiates with --target=my_target
class Target(gvsoc.runner.Target):

    gapy_description = "Single core and memory, running PulpOS"
    model = MyBoard
    name = ""
    config = MyBoardConfig("my_target")
