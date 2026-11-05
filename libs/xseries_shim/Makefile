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
OSI_HARVESTED := $(shell grep -ho -- '-D[A-Za-z_][A-Za-z0-9_]*\(=[^ 	]*\)\?' \
                    $(OSI_MAK) 2>/dev/null | sort -u | tr '\n' ' ')

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
CXXFLAGS ?= -O2 -fPIC -fvisibility=hidden -w
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

# The OS interface keeps osiBus.cpp at its root and the platform-specific
# osiUserCode.cpp one level down (LinuxKernel/ for the kernel-module flavour),
# matching the VPATH in its own .mak. Globbing only the root misses
# osiUserCode.cpp and the link fails on acquireBoard().
#
# Two levels and no deeper, on purpose: LinuxKernel/nirlpk/ holds nirlpk.c,
# which is KERNEL code and must never be compiled into this userspace library.
OSI_SRC = $(wildcard $(OSI_DIR)/*.cpp) $(wildcard $(OSI_DIR)/*/*.cpp)

OBJ = $(DDK_SRC:.cpp=.o) xseries_shim.o

.PHONY: all check install uninstall clean

all: check $(TARGET)

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
