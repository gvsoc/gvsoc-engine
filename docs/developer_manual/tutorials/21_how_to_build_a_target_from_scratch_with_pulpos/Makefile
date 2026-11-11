# SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
#
# SPDX-License-Identifier: Apache-2.0
#
# Authors: Germain Haugou (germain.haugou@gmail.com)

# Root of the SDK, exported by sourceme.sh
GVSOC_ROOT ?= ../../../../../..
TARGET = my_target
# Where the application is compiled and the simulation runs
BUILDDIR ?= $(CURDIR)/build

ifdef GVSOC_WORKDIR
INSTALLDIR ?= $(GVSOC_WORKDIR)/install
else
INSTALLDIR ?= $(GVSOC_ROOT)/install
endif

GVRUN = $(INSTALLDIR)/bin/gvrun --target-dir=$(CURDIR) --target=$(TARGET) --work-dir=$(BUILDDIR)

# Compiles GVSoC with our target, in the SDK build, and the application
build: gvsoc app

gvsoc:
	$(MAKE) -C $(GVSOC_ROOT) TARGETS=$(TARGET) TARGETS_DIRS=$(CURDIR) GVSOC_ONLY=1 build

app:
	$(GVRUN) build

run:
	$(GVRUN) run $(runner_args)

all: build run

clean:
	rm -rf $(BUILDDIR)

.PHONY: build gvsoc app run all clean
