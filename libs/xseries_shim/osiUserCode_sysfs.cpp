/*
 * osiUserCode_sysfs.cpp - an MHDDK OS interface with no kernel module.
 *
 * This replaces NI's LinuxKernel/osiUserCode.cpp, which reaches the card
 * through nirlpk.ko: it opens /dev/nirlpkN, asks the module for BAR0's
 * physical address and size over ioctl, and mmaps through it.
 *
 * That module is 2011 source and does not build against a modern kernel, and
 * NI's only other Linux option - the /dev/mem interface - is 32-bit. Neither
 * is usable on 64-bit Debian 13.
 *
 * So this file does the same job through sysfs instead:
 *
 *      /sys/bus/pci/devices/<slot>/resource0   mmapped directly
 *
 * which needs no kernel module at all. The PCI sysfs resource interface is
 * long-standing stable kernel ABI, so unlike an out-of-tree module this does
 * not rot with each kernel release - which is the whole point of writing it.
 *
 * WHAT IT HAS TO PROVIDE
 *
 * Only four symbols. osiBus.cpp and the inline functions in osiBus.h supply
 * everything else, including iBus::get() and ~tDMAMemory():
 *
 *      acquireBoard()    construct an iBus with BAR0 mapped
 *      releaseBoard()    unmap and tear down
 *      iBus::allocDMA()  refused - see below
 *      iBus::freeDMA()
 *
 * iBus's constructor is private, but acquireBoard() and releaseBoard() are
 * declared its friends in osiBus.h, so this file may construct one and set
 * _physBar and _osSpecific. That is by NI's design, not a trick.
 *
 * NO DMA. Scatter-gather DMA needs physically contiguous kernel memory,
 * which userspace cannot allocate; that is what nirlpk.ko's
 * NIRLP_IOCTL_ALLOCATE_DMA_BUFFER was for. allocDMA() therefore returns NULL.
 * pscan does not use DMA: it reads finite traces straight out of the AI FIFO,
 * following the DDK's aiex2. Anything that does want DMA fails cleanly here
 * rather than corrupting memory.
 *
 * REQUIRES ROOT. Mapping a PCI BAR is privileged.
 *
 * STATUS: not yet run against hardware.
 */

#include "osiBus.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <dirent.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

namespace {

// National Instruments' PCI vendor id.
const unsigned long kNIVendorId = 0x1093;

const char* const kPciDevices = "/sys/bus/pci/devices";

struct tSysfsSpecific
{
   int fileDescriptor;
   void* mappedMemory0;
   unsigned long bar0Size;
   char slot[32];
};

// Read a one-line sysfs attribute holding a hex value such as "0x1093".
bool readHexAttribute(const char* slot, const char* attribute,
                      unsigned long* out)
{
   char path[256];
   snprintf(path, sizeof(path), "%s/%s/%s", kPciDevices, slot, attribute);

   FILE* f = fopen(path, "r");
   if (f == NULL) return false;

   char buffer[64];
   const bool ok = (fgets(buffer, sizeof(buffer), f) != NULL);
   fclose(f);
   if (!ok) return false;

   *out = strtoul(buffer, NULL, 16);
   return true;
}

/*
 * BAR0's physical start and size, from the device's "resource" attribute.
 *
 * Each line is "<start> <end> <flags>" in hex, one per BAR, so line 0 is
 * BAR0 and the size is end - start + 1. An unimplemented BAR reads as all
 * zeroes, which is why a zero size is treated as a failure.
 */
bool readBar0(const char* slot, unsigned long* start, unsigned long* size)
{
   char path[256];
   snprintf(path, sizeof(path), "%s/%s/resource", kPciDevices, slot);

   FILE* f = fopen(path, "r");
   if (f == NULL) return false;

   unsigned long long barStart = 0, barEnd = 0, flags = 0;
   const int fields = fscanf(f, "%llx %llx %llx", &barStart, &barEnd, &flags);
   fclose(f);

   if (fields != 3 || barEnd <= barStart) return false;

   *start = static_cast<unsigned long>(barStart);
   *size = static_cast<unsigned long>(barEnd - barStart + 1);
   return true;
}

/*
 * Find the sysfs slot for a bus and device number.
 *
 * The DDK's resource string carries them in DECIMAL ("PXI130::0::INSTR" for
 * the card at 82:00.0), while sysfs names slots in HEX. Both are parsed to
 * integers and compared numerically so the two conventions cannot be
 * confused - mixing them up silently opens a different card.
 *
 * Only function 0 is considered, and the vendor id is checked, so a
 * non-NI device at the same address cannot be opened by mistake.
 */
bool findSlot(unsigned long bus, unsigned long device,
              char* slot, size_t slotLen)
{
   DIR* dir = opendir(kPciDevices);
   if (dir == NULL) return false;

   bool found = false;
   struct dirent* entry;

   while (!found && (entry = readdir(dir)) != NULL)
   {
      unsigned int domain = 0, entryBus = 0, entryDev = 0, entryFunc = 0;
      if (sscanf(entry->d_name, "%x:%x:%x.%x",
                 &domain, &entryBus, &entryDev, &entryFunc) != 4)
      {
         continue;
      }

      if (entryBus != bus || entryDev != device || entryFunc != 0) continue;

      unsigned long vendor = 0;
      if (!readHexAttribute(entry->d_name, "vendor", &vendor)) continue;
      if (vendor != kNIVendorId) continue;

      snprintf(slot, slotLen, "%s", entry->d_name);
      found = true;
   }

   closedir(dir);
   return found;
}

/*
 * Turn the device on.
 *
 * With no driver bound, the card's PCI command register may have memory
 * decoding disabled, in which case every register read comes back as 0xFFFF
 * and nothing announces why. Writing "1" to the enable attribute sets it.
 * Harmless if it is already enabled.
 */
void enableDevice(const char* slot)
{
   char path[256];
   snprintf(path, sizeof(path), "%s/%s/enable", kPciDevices, slot);

   FILE* f = fopen(path, "w");
   if (f == NULL) return;
   fputs("1\n", f);
   fclose(f);
}

}  // namespace

iBus* acquireBoard(tChar* brdLocation)
{
   unsigned int busNumber = 0;
   unsigned int devNumber = 0;

   if (sscanf(brdLocation, "PXI%u::%u::INSTR", &busNumber, &devNumber) != 2)
   {
      printf("invalid device descriptor: %s\n", brdLocation);
      return NULL;
   }

   char slot[32];
   if (!findSlot(busNumber, devNumber, slot, sizeof(slot)))
   {
      printf("no National Instruments device at PCI bus %u device %u "
             "(looked in %s for an entry with vendor 0x%04lx)\n",
             busNumber, devNumber, kPciDevices, kNIVendorId);
      return NULL;
   }

   enableDevice(slot);

   unsigned long physicalBar0 = 0;
   unsigned long bar0Size = 0;
   if (!readBar0(slot, &physicalBar0, &bar0Size))
   {
      printf("could not read BAR0 geometry for %s\n", slot);
      return NULL;
   }

   char resourcePath[256];
   snprintf(resourcePath, sizeof(resourcePath), "%s/%s/resource0",
            kPciDevices, slot);

   // O_SYNC so the mapping is not write-buffered: these are device
   // registers, and a deferred write would reorder against a status read.
   const int fd = open(resourcePath, O_RDWR | O_SYNC);
   if (fd < 0)
   {
      printf("could not open %s - mapping a PCI BAR needs root\n",
             resourcePath);
      return NULL;
   }

   // Offset 0: a sysfs resourceN file is the BAR itself, so the mapping
   // starts at its base rather than at the physical address.
   void* mem0 = mmap(NULL, bar0Size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
   if (mem0 == MAP_FAILED)
   {
      printf("could not mmap %s (%lu bytes)\n", resourcePath, bar0Size);
      close(fd);
      return NULL;
   }

   iBus* bus = new iBus(devNumber, 0, mem0, NULL);

   bus->_physBar[0] = static_cast<u32>(physicalBar0);
   for (int i = 1; i < 6; ++i) bus->_physBar[i] = 0;

   tSysfsSpecific* specific = new tSysfsSpecific;
   specific->fileDescriptor = fd;
   specific->mappedMemory0 = mem0;
   specific->bar0Size = bar0Size;
   snprintf(specific->slot, sizeof(specific->slot), "%s", slot);

   bus->_osSpecific = reinterpret_cast<void*>(specific);
   return bus;
}

void releaseBoard(iBus*& bus)
{
   if (bus == NULL) return;

   tSysfsSpecific* specific =
      reinterpret_cast<tSysfsSpecific*>(bus->_osSpecific);

   if (specific != NULL)
   {
      munmap(specific->mappedMemory0, specific->bar0Size);
      close(specific->fileDescriptor);
      delete specific;
   }

   // The device is deliberately left enabled: disabling it would drop the
   // analog outputs, and a scan ending must not slam a mirror or piezo back
   // through its range.
   delete bus;
   bus = NULL;
}

/*
 * DMA is not available without a kernel module - see the header comment.
 * Returning NULL makes any caller that wants it fail at the allocation
 * rather than proceeding with an invalid buffer.
 */
tDMAMemory* iBus::allocDMA(u32 size)
{
   nNIOSINT100_mUnused(size);
   return NULL;
}

void iBus::freeDMA(tDMAMemory* mem)
{
   delete mem;
}
