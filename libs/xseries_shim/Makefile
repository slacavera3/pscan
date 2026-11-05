# Build libpscan_xseries.so - pscan's shim over NI's X Series DDK.
#
# The DDK and its OS interface cannot be redistributed, so neither is in this
# repository. Both are dropped into libs/vendor/ (gitignored) and install.sh
# asks for them by name and URL when they are missing.
#
# Usage, normally via install.sh:
#     make NIXSERIES_DIR=../vendor/nixseries OSI_DIR=../vendor/osinterface
#     sudo make install
#
# Everything compiled here is NI's source plus xseries_shim.cpp. The result is
# a plain shared library that pscan/nixseries_driver.py loads with ctypes, the
# same arrangement this repo already uses for libcomedi, libphidget21 and
# libTUCam.

NIXSERIES_DIR ?= ../vendor/nixseries
OSI_DIR       ?= ../vendor/osinterface
PREFIX        ?= /usr/local

TARGET  = libpscan_xseries.so
SONAME  = $(TARGET).1

# The DDK's examples include their helpers as "inTimer/aiHelper.h" and the OS
# interface as "osiBus.h", so both roots go on the include path.
INCLUDES = \
	-I$(NIXSERIES_DIR) \
	-I$(NIXSERIES_DIR)/ChipObjects \
	-I$(NIXSERIES_DIR)/Examples \
	-I$(NIXSERIES_DIR)/CHInCh \
	-I$(OSI_DIR)

# Defines the DDK itself requires, taken from nixseries/Makefile:
#   kBAR0Only    - this card's registers are all in BAR0
#   k64BitKernel - the DDK's own "Enable 64-bit support" switch
DDK_DEFINES = -DkBAR0Only=1 -Dk64BitKernel=1

# osiTypes.h refuses to compile without knowing the target platform and
# endianness:
#     #error target endianness not specified
#     #error platform not specified; see osiTypes.h
#
# Those selections live in the OS interface's own .mak, which the DDK's
# Makefile pulls in with `include $(OSINTERFACE_DIR)/$(OSINTERFACE_MAKEFILE)`.
# Including that file wholesale here would also drag in its build rules and
# fight ours, so instead the -D flags are harvested out of it. OSI_DEFINES can
# be set by hand to override the harvest.
OSI_MAK ?= $(firstword $(wildcard $(OSI_DIR)/*.mak))

# /dev/null is passed as a second file deliberately, and the whole thing is
# guarded on OSI_MAK being non-empty.
#
# grep with NO file argument reads STDIN and blocks forever. Inside
# $(shell ...) that hangs make during variable expansion - before it can print
# a recipe line, so it looks like a silent freeze with no clue as to the
# cause. That happened whenever OSI_DIR pointed somewhere without a .mak.
OSI_HARVESTED := $(if $(OSI_MAK),$(shell \
   grep -ho -- '-D[A-Za-z_][A-Za-z0-9_]*\(=[^ 	]*\)\?' \
        $(OSI_MAK) /dev/null 2>/dev/null | sort -u | tr '\n' ' '),)

# What LinuxKernel.mak actually specifies, used when the harvest finds
# nothing - a differently named .mak, or one that sets the flags some other
# way. osiTypes.h tests kLittleEndian/kBigEndian for endianness and
# kMicrosoft/kApple/kGNU for platform, so this pair is right for any
# little-endian Linux built with GCC.
OSI_FALLBACK = -DkLittleEndian=1 -DkGNU=1

OSI_DEFINES ?= $(if $(strip $(OSI_HARVESTED)),$(OSI_HARVESTED),$(OSI_FALLBACK))

# -fPIC because this becomes a shared library. The DDK is 2011-era C++ and
# trips newer warnings that are not ours to fix, so its noise is quietened
# while our own file is compiled with warnings on (see shim.o below).
#
# -fpermissive is needed for a real defect in NI's headers, not just noise:
#
#   tOutTimer.h:39  ((fieldId) << 27)
#   tDO.h:1478      kkDIODirection_Pin31Id -> fieldId 32
#
# 32 << 27 is 2^32, which overflows a 32-bit int, so that one enumerator is
# not a constant expression. GCC used to warn and now errors. It has been
# wrong since 2011 and only affects the DIO direction register's pin 31 field
# id, which nothing here uses - but tDO.cpp still has to compile, because
# tXSeries aggregates the DI and DO chip objects.
CXXFLAGS ?= -O2 -fPIC -fvisibility=hidden -w -fpermissive
CXXFLAGS += $(DDK_DEFINES) $(OSI_DEFINES)
LDFLAGS  ?= -shared -Wl,-soname,$(SONAME)

# NI's chip objects and the example helpers the shim calls into. The DMA
# sources under CHInCh/ are deliberately absent: the /dev/mem OS interface
# cannot do DMA and the shim's FIFO path does not need it.
DDK_SRC = \
	$(wildcard $(NIXSERIES_DIR)/ChipObjects/*.cpp) \
	$(NIXSERIES_DIR)/Examples/devices.cpp \
	$(NIXSERIES_DIR)/Examples/dataHelper.cpp \
	$(NIXSERIES_DIR)/Examples/eepromHelper.cpp \
	$(NIXSERIES_DIR)/Examples/simultaneousInit.cpp \
	$(NIXSERIES_DIR)/Examples/streamHelper.cpp \
	$(NIXSERIES_DIR)/Examples/inTimer/aiHelper.cpp \
	$(NIXSERIES_DIR)/Examples/inTimer/inTimerHelper.cpp \
	$(NIXSERIES_DIR)/Examples/inTimer/inTimerParams.cpp \
	$(NIXSERIES_DIR)/Examples/outTimer/aoHelper.cpp \
	$(NIXSERIES_DIR)/Examples/outTimer/outTimerHelper.cpp

# Which implementation of acquireBoard()/releaseBoard() to build against.
#
#   USE_SYSFS_BUS=1 (default)
#     osiUserCode_sysfs.cpp, ours. Maps the card's BAR through
#     /sys/bus/pci/devices/<slot>/resource0 and needs NO kernel module.
#
#   USE_SYSFS_BUS=0
#     NI's LinuxKernel/osiUserCode.cpp, which talks to nirlpk.ko over ioctl
#     and so needs that module built and loaded. Available as a fallback if
#     the sysfs route ever disappoints; it also enables DMA, which ours
#     cannot do.
#
# Either way osiBus.cpp from the OS interface root is used unchanged - it
# supplies iBus::get() and ~tDMAMemory().
USE_SYSFS_BUS ?= 1

ifeq ($(USE_SYSFS_BUS),1)
  OSI_SRC = $(wildcard $(OSI_DIR)/*.cpp) osiUserCode_sysfs.cpp
else
  # Two levels and no deeper, on purpose: LinuxKernel/nirlpk/ holds nirlpk.c,
  # which is KERNEL code and must never reach this userspace library.
  OSI_SRC = $(wildcard $(OSI_DIR)/*.cpp) $(wildcard $(OSI_DIR)/*/*.cpp)
endif

OBJ = $(DDK_SRC:.cpp=.o) xseries_shim.o

.PHONY: all check verify-exports install uninstall clean

all: check $(TARGET) verify-exports

# A library that builds, links and loads but exports nothing is the worst
# kind of success: everything looks fine until ctypes reports "undefined
# symbol: xseries_open" at first use, a long way from the cause. Checking
# here turns that into a build failure naming the likely reason.
verify-exports: $(TARGET)
	@nm -D --defined-only $(TARGET) 2>/dev/null | grep -q 'T xseries_open' || { \
		echo ""; \
		echo "ERROR: $(TARGET) does not export xseries_open."; \
		echo "       It built and linked, but nothing can call into it."; \
		echo "       The usual cause is -fvisibility=hidden without the"; \
		echo "       '#pragma GCC visibility push(default)' around the"; \
		echo "       extern \"C\" API block in xseries_shim.cpp."; \
		exit 1; }
	@echo "  Exports: $$(nm -D --defined-only $(TARGET) | grep -c 'T xseries_') xseries_* entry points"

# Fail with something actionable rather than 200 lines of "no such file".
check:
	@test -d "$(NIXSERIES_DIR)/ChipObjects" || { \
		echo "ERROR: no X Series DDK at $(NIXSERIES_DIR)"; \
		echo "       Expected $(NIXSERIES_DIR)/ChipObjects/tXSeries.h"; \
		echo "       Drop nixseries.tar.gz in libs/vendor/ and re-run"; \
		echo "       'bash install.sh', which extracts it for you."; \
		exit 1; }
	@test -f "$(OSI_DIR)/osiBus.h" || { \
		echo "ERROR: no MHDDK OS interface at $(OSI_DIR)"; \
		echo "       Expected $(OSI_DIR)/osiBus.h"; \
		echo "       This is a SEPARATE download from the DDK itself -"; \
		echo "       take 'Linux (via kernel module)' VERSION 2.0, the"; \
		echo "       only 64-bit-capable Linux option."; \
		exit 1; }
	@echo "  DDK defines: $(DDK_DEFINES)"
	@echo "  OSI defines: $(OSI_DEFINES)$(if $(strip $(OSI_HARVESTED)), (from $(notdir $(OSI_MAK))), (fallback - no -D flags found in $(OSI_DIR)/*.mak))"
	@echo "  Bus access : $(if $(filter 1,$(USE_SYSFS_BUS)),sysfs resource0 mmap - NO kernel module,NI nirlpk.ko via ioctl)"
	@echo "  OSI sources: $(notdir $(OSI_SRC))"
	@test -n "$(strip $(OSI_SRC))" || { \
		echo "ERROR: found no OS interface sources under $(OSI_DIR)"; \
		echo "       Expected osiBus.cpp there and osiUserCode.cpp one"; \
		echo "       level down."; \
		exit 1; }

# Our own file gets warnings on; NI's 2011 sources do not, since their
# warnings are not actionable here.
xseries_shim.o: xseries_shim.cpp
	$(CXX) $(CXXFLAGS) -Wall -Wextra $(INCLUDES) -c $< -o $@

%.o: %.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

# CXXFLAGS matters here, not just LDFLAGS: OSI_SRC is handed to g++ as source,
# so it is compiled by this command. Without the defines it would hit
# osiTypes.h's "#error target endianness not specified" at link time, having
# already compiled cleanly everywhere else.
$(TARGET): $(OBJ) $(OSI_SRC)
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(LDFLAGS) -o $@ $(OBJ) $(OSI_SRC)

install: $(TARGET)
	install -d $(PREFIX)/lib
	install -m 0755 $(TARGET) $(PREFIX)/lib/$(SONAME)
	ln -sf $(SONAME) $(PREFIX)/lib/$(TARGET)
	/sbin/ldconfig || ldconfig

uninstall:
	rm -f $(PREFIX)/lib/$(TARGET) $(PREFIX)/lib/$(SONAME)
	/sbin/ldconfig || ldconfig

clean:
	rm -f $(OBJ) $(TARGET)
