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

# -fPIC because this becomes a shared library. The DDK is 2011-era C++ and
# trips newer warnings that are not ours to fix, so its noise is quietened
# while our own file is compiled with warnings on (see shim.o below).
CXXFLAGS ?= -O2 -fPIC -fvisibility=hidden -w
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

OSI_SRC = $(wildcard $(OSI_DIR)/*.cpp) $(wildcard $(OSI_DIR)/*.c)

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
		echo "       take the Linux /dev/mem flavour, which needs no"; \
		echo "       kernel module."; \
		exit 1; }

# Our own file gets warnings on; NI's 2011 sources do not, since their
# warnings are not actionable here.
xseries_shim.o: xseries_shim.cpp
	$(CXX) $(CXXFLAGS) -Wall -Wextra $(INCLUDES) -c $< -o $@

%.o: %.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

$(TARGET): $(OBJ) $(OSI_SRC)
	$(CXX) $(LDFLAGS) -o $@ $(OBJ) $(OSI_SRC) $(INCLUDES)

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
