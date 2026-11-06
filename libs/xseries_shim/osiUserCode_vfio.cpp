/*
 * osiUserCode_vfio.cpp - an MHDDK OS interface built on vfio-pci.
 *
 * This replaces NI's LinuxKernel/osiUserCode.cpp, which reaches the card
 * through nirlpk.ko - 2011 source that does not build against a modern
 * kernel, and could not be loaded if it did, because kernel lockdown refuses
 * unsigned modules.
 *
 * WHY VFIO AND NOT A RAW BAR MMAP
 *
 * The obvious alternative, mmapping /sys/bus/pci/devices/<slot>/resource0,
 * fails with EPERM on any machine with Secure Boot: that turns on kernel
 * lockdown, which blocks userspace PCI BAR access outright
 * (LOCKDOWN_PCI_ACCESS). Root does not override it and it cannot be lowered
 * at runtime, so the only way out would be a BIOS change - not something an
 * installer can do, nor something to ask a new user to do.
 *
 * vfio-pci is permitted under lockdown because the IOMMU contains anything
 * the device tries to DMA, so a misbehaving card cannot reach kernel memory.
 * It is in-tree and signed, needs no BIOS change, and - with a udev rule on
 * /dev/vfio - needs no root either, which the raw mmap always would have.
 *
 * Verified on a PCIe-6351 under Debian 13 with lockdown in integrity mode.
 *
 * WHAT IT PROVIDES
 *
 * The same four symbols NI's version does; osiBus.cpp and the inlines in
 * osiBus.h supply everything else:
 *
 *      acquireBoard()    open the card through VFIO, BAR0 mapped
 *      releaseBoard()    unmap and close
 *      iBus::allocDMA()  refused - see below
 *      iBus::freeDMA()
 *
 * iBus's constructor is private, but acquireBoard() and releaseBoard() are
 * declared its friends in osiBus.h, so this file may construct one and set
 * _physBar and _osSpecific. That is NI's design, not a trick.
 *
 * NO DMA. VFIO can do DMA, but it needs the buffer mapped into the IOMMU
 * (VFIO_IOMMU_MAP_DMA) and the DDK's CHInCh scatter-gather engine driven
 * over it, which is a project in itself. pscan does not need it: it reads
 * finite traces straight out of the AI FIFO, following the DDK's aiex2.
 * allocDMA() returns NULL so anything that does want DMA fails cleanly.
 *
 * REQUIRES the card bound to vfio-pci. install.sh arranges that persistently
 * through /etc/modprobe.d; the error messages below say so if it has not
 * happened.
 */

#include "osiBus.h"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <dirent.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <linux/vfio.h>

namespace {

// National Instruments' PCI vendor id.
const unsigned long kNIVendorId = 0x1093;

const char* const kPciDevices = "/sys/bus/pci/devices";

struct tVfioSpecific
{
   int containerFd;
   int groupFd;
   int deviceFd;
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
 * Find the sysfs slot for a bus and device number.
 *
 * The DDK's resource string carries them in DECIMAL ("PXI130::0::INSTR" for
 * the card at 82:00.0) while sysfs names slots in HEX, so both are parsed to
 * integers and compared numerically. Mixing the two conventions up silently
 * opens a different card.
 *
 * Only function 0 is considered and the vendor id is checked, so a non-NI
 * device at the same address cannot be opened by mistake.
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

// The card's IOMMU group number, from the sysfs symlink. Empty when the
// IOMMU is off, which VFIO cannot work without.
bool iommuGroup(const char* slot, char* group, size_t groupLen)
{
   char path[256];
   snprintf(path, sizeof(path), "%s/%s/iommu_group", kPciDevices, slot);

   char target[256];
   const ssize_t len = readlink(path, target, sizeof(target) - 1);
   if (len < 0) return false;
   target[len] = '\0';

   const char* base = strrchr(target, '/');
   snprintf(group, groupLen, "%s", base ? base + 1 : target);
   return true;
}

void closeIfOpen(int fd)
{
   if (fd >= 0) close(fd);
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
             "(looked in %s for vendor 0x%04lx)\n",
             busNumber, devNumber, kPciDevices, kNIVendorId);
      return NULL;
   }

   char group[32];
   if (!iommuGroup(slot, group, sizeof(group)))
   {
      printf("%s has no IOMMU group, so vfio-pci cannot be used.\n", slot);
      printf("   Add 'intel_iommu=on' to the kernel command line\n"
             "   (/etc/default/grub, then update-grub) and reboot.\n");
      return NULL;
   }

   int containerFd = -1;
   int groupFd = -1;
   int deviceFd = -1;

   containerFd = open("/dev/vfio/vfio", O_RDWR);
   if (containerFd < 0)
   {
      printf("could not open /dev/vfio/vfio: %s\n", strerror(errno));
      printf("   Load the module with 'sudo modprobe vfio-pci'.\n");
      return NULL;
   }

   if (ioctl(containerFd, VFIO_GET_API_VERSION) != VFIO_API_VERSION)
   {
      printf("unexpected VFIO API version\n");
      closeIfOpen(containerFd);
      return NULL;
   }

   if (!ioctl(containerFd, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU))
   {
      printf("this kernel's VFIO has no type1 IOMMU support\n");
      closeIfOpen(containerFd);
      return NULL;
   }

   char groupPath[64];
   snprintf(groupPath, sizeof(groupPath), "/dev/vfio/%s", group);

   groupFd = open(groupPath, O_RDWR);
   if (groupFd < 0)
   {
      printf("could not open %s: %s\n", groupPath, strerror(errno));
      if (errno == ENOENT)
      {
         printf("   The card is not bound to vfio-pci. install.sh sets this\n"
                "   up persistently; to do it by hand:\n"
                "     echo vfio-pci | sudo tee %s/%s/driver_override\n"
                "     echo %s | sudo tee /sys/bus/pci/drivers_probe\n",
                kPciDevices, slot, slot);
      }
      else if (errno == EACCES || errno == EPERM)
      {
         printf("   No permission. install.sh installs a udev rule giving\n"
                "   the 'pscan' group access to /dev/vfio; you may need to\n"
                "   log out and back in for your group membership to apply.\n");
      }
      closeIfOpen(containerFd);
      return NULL;
   }

   struct vfio_group_status groupStatus;
   memset(&groupStatus, 0, sizeof(groupStatus));
   groupStatus.argsz = sizeof(groupStatus);

   if (ioctl(groupFd, VFIO_GROUP_GET_STATUS, &groupStatus) < 0)
   {
      printf("VFIO_GROUP_GET_STATUS failed: %s\n", strerror(errno));
      closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   if (!(groupStatus.flags & VFIO_GROUP_FLAGS_VIABLE))
   {
      // Every device in an IOMMU group has to move to vfio-pci together,
      // because the group is the isolation boundary the IOMMU enforces.
      printf("IOMMU group %s is not viable: another device in the same\n"
             "   group is still bound to its normal driver. List the group\n"
             "   with:\n"
             "     ls /sys/kernel/iommu_groups/%s/devices\n", group, group);
      closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   if (ioctl(groupFd, VFIO_GROUP_SET_CONTAINER, &containerFd) < 0 ||
       ioctl(containerFd, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU) < 0)
   {
      printf("could not attach the group to a VFIO container: %s\n",
             strerror(errno));
      closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   deviceFd = ioctl(groupFd, VFIO_GROUP_GET_DEVICE_FD, slot);
   if (deviceFd < 0)
   {
      printf("VFIO_GROUP_GET_DEVICE_FD for %s failed: %s\n",
             slot, strerror(errno));
      closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   /*
    * Put the card into its power-on state before touching it.
    *
    * Nothing has ever initialised this device: no NI driver has run on this
    * machine, and the DDK's examples assume a card in a known state rather
    * than whatever the BIOS left behind. A function-level reset is the one
    * initialisation step available to us that the DDK does not perform
    * itself.
    *
    * Failure is not fatal - not every device supports reset, and a card
    * that is already in a sane state does not need it.
    */
   if (ioctl(deviceFd, VFIO_DEVICE_RESET) < 0)
   {
      if (getenv("PSCAN_XSERIES_DEBUG") != NULL)
         printf("[xseries] VFIO_DEVICE_RESET not available: %s\n",
                strerror(errno));
   }
   else if (getenv("PSCAN_XSERIES_DEBUG") != NULL)
   {
      printf("[xseries] device reset ok\n");
   }

   struct vfio_region_info region;
   memset(&region, 0, sizeof(region));
   region.argsz = sizeof(region);
   region.index = VFIO_PCI_BAR0_REGION_INDEX;

   if (ioctl(deviceFd, VFIO_DEVICE_GET_REGION_INFO, &region) < 0)
   {
      printf("VFIO_DEVICE_GET_REGION_INFO for BAR0 failed: %s\n",
             strerror(errno));
      closeIfOpen(deviceFd); closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   if (region.size == 0 || !(region.flags & VFIO_REGION_INFO_FLAG_MMAP))
   {
      printf("BAR0 is not mappable (size %llu, flags 0x%x)\n",
             static_cast<unsigned long long>(region.size), region.flags);
      closeIfOpen(deviceFd); closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   // Mapped from the DEVICE fd at the region's own offset, not from a sysfs
   // resource file - that indirection is what the IOMMU sits behind.
   void* mem0 = mmap(NULL, region.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                     deviceFd, region.offset);
   if (mem0 == MAP_FAILED)
   {
      printf("could not mmap BAR0 through VFIO (%llu bytes): %s\n",
             static_cast<unsigned long long>(region.size), strerror(errno));
      closeIfOpen(deviceFd); closeIfOpen(groupFd); closeIfOpen(containerFd);
      return NULL;
   }

   iBus* bus = new iBus(devNumber, 0, mem0, NULL);

   // The DDK wants BAR0's physical address for the MITE on older hardware.
   // X Series does not use it, and VFIO deliberately does not expose it, so
   // it is left zero rather than invented.
   for (int i = 0; i < 6; ++i) bus->_physBar[i] = 0;

   tVfioSpecific* specific = new tVfioSpecific;
   specific->containerFd = containerFd;
   specific->groupFd = groupFd;
   specific->deviceFd = deviceFd;
   specific->mappedMemory0 = mem0;
   specific->bar0Size = static_cast<unsigned long>(region.size);
   snprintf(specific->slot, sizeof(specific->slot), "%s", slot);

   bus->_osSpecific = reinterpret_cast<void*>(specific);
   return bus;
}

void releaseBoard(iBus*& bus)
{
   if (bus == NULL) return;

   tVfioSpecific* specific =
      reinterpret_cast<tVfioSpecific*>(bus->_osSpecific);

   if (specific != NULL)
   {
      munmap(specific->mappedMemory0, specific->bar0Size);

      // Innermost first: the device fd is held by the group, which is held
      // by the container. Closing out of order leaves the group attached.
      closeIfOpen(specific->deviceFd);
      closeIfOpen(specific->groupFd);
      closeIfOpen(specific->containerFd);

      delete specific;
   }

   // The card stays bound to vfio-pci and its outputs keep their last
   // values. A scan ending must not slam a mirror or piezo through its
   // range, and rebinding would reset the device.
   delete bus;
   bus = NULL;
}

/*
 * DMA is not implemented - see the header comment. Returning NULL makes a
 * caller that wants it fail at the allocation rather than proceeding with an
 * invalid buffer.
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
