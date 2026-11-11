# SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

# The application, compiled by gvrun for the target given with --target

import os
import sys

# The PulpOS port is next to this file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pulpos_port import MyTargetExecutable


def declare(target):

    test = MyTargetExecutable('test', target)

    test.set_optimization_level('-Os -g')
    test.add_sources('test.c')
