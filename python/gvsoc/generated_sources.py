#
# Copyright (C) 2026 ETH Zurich and University of Bologna
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Sources of the components compiled from Python (generated components).

Their module name is derived from their sources, and all of them are installed
in the same models directory. Tests and tutorials give bare file names
(``test.cpp``, ``stub_master.cpp``) found next to their Python file, and
several directories use the same names for different code: the bare names are
made absolute here so that each directory gets its own module.
"""

import os
import re


def qualify_sources(sources: list[str], caller_file: str | None) -> list[str]:
    """Bare file names found next to the Python file adding them, as absolute
    paths. The others, relative to the target directories, are kept."""
    if caller_file is None:
        return list(sources)
    caller_dir = os.path.dirname(os.path.realpath(caller_file))
    result = []
    for source in sources:
        if os.path.dirname(source) == '':
            path = os.path.join(caller_dir, source)
            if os.path.exists(path):
                source = path
        result.append(source)
    return result


def component_label(source: str) -> str:
    """The readable part of a generated component name, from its first source:
    the path for a source relative to the target directories, the end of the
    path (three directories and the file) for an absolute one."""
    if os.path.isabs(source):
        source = '/'.join(source.split(os.sep)[-4:])
        return re.sub(r'[^A-Za-z0-9_]', '_', source)
    return source.replace('/', '_').replace('.', '_')
