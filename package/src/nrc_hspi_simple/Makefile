# SPDX-License-Identifier: GPL-2.0
KDIR ?= /lib/modules/$(shell uname -r)/build
CHECKPATCH ?= $(KDIR)/scripts/checkpatch.pl

include $(KDIR)/.config

M_SRC = nrc_simple

obj-m += $(M_SRC).o
$(M_SRC)-objs = \
		nrc_hspi_simple_driver.o

ccflags-y := \
	-DDEBUG

ifeq ($(TESTMODE), y)
ccflags-y += \
		-DTEST_SELF_GENERATION
endif

ccflags-y += -Wall -Werror

all: modules

cleanall: clean all

modules:
	@echo $(NRCLINUXHOSTDIR)
	@$(MAKE) -C $(KDIR) M=$$PWD modules

modules_install:
	@$(MAKE) -C $(KDIR) M=$$PWD modules_install

install: modules
	@sudo insmod $(M_SRC).ko

test: modules
	@sudo insmod $(M_SRC).ko
	@sudo rmmod -f $(M_SRC)

doxyfile.inc:
	@echo INPUT         =  $(NRCLINUXHOSTDIR) > doxyfile.inc
	@echo EXTRACT_ALL   = yes >> doxyfile.inc
	@echo EXTRACT_STATIC = yes >> doxyfile.inc
	@echo INLINE_SOURCES = yes >> doxyfile.inc
	@echo HAVE_DOT = yes = yes >> doxyfile.inc

doxy: doxyfile.inc
	@doxygen doxyfile.inc

clean:
	@rm -f *.o
	@rm -f *.ko
	@rm -f .*.cmd
	@rm -f *.mod *.mod.c
	@rm -f modules.order
	@rm -f Module.symvers
	@rm -rf .tmp_versions
	@rm -rf html
	@rm -rf latex
	@rm -f doxyfile.inc

check: checkstyle

checkstyle:
	@$(CHECKPATCH) --root=$(KDIR) -file -terse --ignore=LINUX_VERSION_CODE -no-tree *.c *.h

checkstyle2:
	@if [ $$($(CHECKPATCH) --root=$(KDIR) -file --quiet -no-tree *.c *.h | awk '{if($$1 == "ERROR:") print $$1;}' | wc -l) -ne 0 ]; \
	then \
		echo >&2 "checkstyle2 error"; \
		false; \
	fi
