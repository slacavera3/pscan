/*
 * xseries_shim.cpp - a narrow C API over NI's X Series DDK, for pscan.
 *
 * WHY THIS EXISTS
 *
 * NI-DAQmx has no Debian support, and comedi's ni_pcimio driver covers only
 * the E and M series - an X Series board such as the PCIe-6351 appears in
 * neither. NI's Measurement Hardware Driver Development Kit (MHDDK) is the
 * route they do offer Linux users: it documents the DAQ-STC3 registers and
 * ships C++ "chip objects" for them.
 *
 * This file wraps exactly what pscan needs from that kit - acquire one
 * hardware-timed finite analog input trace, and hold one analog output at a
 * voltage - behind a flat extern "C" API that ctypes can call. Everything
 * else the DDK can do is deliberately absent.
 *
 * NO DMA IS USED. The AI path below reads straight out of the AI data FIFO,
 * following the DDK's own aiex2 example, so a finite trace must fit in that
 * FIFO - which at pscan's ~20 KB/s and 1000-sample traces it comfortably
 * does.
 *
 * This file is agnostic about which of the DDK's Linux OS interfaces is
 * underneath: both provide acquireBoard() and createAddressSpace(). On a
 * 64-bit machine that has to be "Linux (via kernel module)" version 2.0,
 * which builds nirlpk.ko - NI ships it as source under the BSD licence. The
 * /dev/mem interface would avoid the kernel module but is 32-bit only and
 * targets kernel 2.4.
 *
 * CORRESPONDENCE TO THE DDK EXAMPLES
 *
 *   xseries_read_ai()  <- Examples/aiex2.cpp (finite hardware-timed AI, read
 *                         from FIFO), changed to a software start trigger
 *                         (aiex3's kStart1_SW_Pulse + strobeStart1) instead
 *                         of PFI0, and to one channel instead of four.
 *   xseries_write_ao() <- Examples/aoex1.cpp (single-point on-demand AO,
 *                         written directly to the DAC data register).
 *
 * Scaling in both directions goes through the DDK's own data helpers, which
 * apply the calibration constants read from the device EEPROM. Raw ADC codes
 * scaled without them are off by around a percent.
 *
 * BUILD
 *
 * Needs NI's DDK source and one of its OS interfaces, neither of which may be
 * redistributed, so neither is in this repository. See ../vendor/README and
 * the Makefile beside this file. install.sh asks for both by name and URL.
 *
 * STATUS: written against the DDK sources and examples but NOT yet compiled
 * or run against hardware. Treat every register sequence here as unverified.
 */

#include <cstdarg>   // va_list, for set_err()
#include <cstdio>
#include <cstdlib>   // getenv(), for the debug switch
#include <cstring>
#include <cmath>     // llround
#include <ctime>     // clock(), CLOCKS_PER_SEC
#include <vector>

// OS Interface: iBus, acquireBoard(), releaseBoard(), tAddressSpace
#include "osiBus.h"
#include "osiTypes.h"

// Chip Objects
#include "tXSeries.h"

// Chip Object Helpers (from the DDK's Examples/ tree)
#include "dataHelper.h"
#include "devices.h"
#include "eepromHelper.h"
#include "inTimer/aiHelper.h"
#include "outTimer/aoHelper.h"
#include "simultaneousInit.h"
#include "streamHelper.h"

// The DAQ-STC3 timebase the sample and convert periods are counted in.
static const double kTimebaseHz = 100.0e6;

// Minimum delays, in timebase ticks, between the start trigger and the first
// sample clock, and between the sample clock and the convert. The DDK notes
// both must be >= 2.
static const u32 kSampleDelay = 2;
static const u32 kConvertDelay = 2;

// Convert clock period for multiplexed (non-simultaneous) devices. 100 MHz /
// 400 = 250 kHz, which the DDK examples use as a safe default. The sample
// period must leave room for one convert per channel.
static const u32 kConvertPeriod = 400;

// How long to spin on a status register before giving up.
static const double kRlpTimeoutS = 5.0;

struct xseries_session
{
   iBus* bus;
   tAddressSpace bar0;
   tXSeries* device;
   const nNISTC3::tDeviceInfo* info;
   nNISTC3::eepromHelper* eeprom;

   // The AO helper is kept alive for the whole session on purpose. Its
   // constructor/reset path drives the outputs, so building one per write
   // would glitch a mirror or piezo back through zero on every scan step.
   nNISTC3::aoHelper* ao;
   bool aoConfigured[32];
   u32 aoConfiguredRange[32];
   //: Last DAC code written per channel, or -1 if none yet. Kept so the
   //: shadow register can be checked against it later - see
   //: xseries_ao_shadow(). memset(0) in xseries_open() would make that look
   //: like "0 was written", so it is set to -1 explicitly there.
   i32 aoLastCode[32];
   double aoLastVolts[32];

   char err[512];
};

extern "C" {

struct xseries_device_info
{
   u32 product_id;
   char name[64];
   u32 is_simultaneous;
   u32 n_ai_channels;
   u32 n_ao_channels;
   u32 n_adcs;
   u32 n_dacs;
   u32 ai_fifo_samples;   // largest finite trace the FIFO path can hold
};

}  // extern "C"

static void set_err(xseries_session* s, const char* fmt, ...)
{
   if (s == NULL) return;
   va_list args;
   va_start(args, fmt);
   vsnprintf(s->err, sizeof(s->err), fmt, args);
   va_end(args);
}

/*
 * PSCAN_XSERIES_DEBUG=1 dumps the acquisition's internal state.
 *
 * A trace that comes back as the same value repeated could be the FIFO not
 * advancing, the timing engine not converting, or the FIFO status register
 * being misread - and the finished volts cannot tell those apart. This
 * prints the raw codes and the registers the decisions were made on, so one
 * run distinguishes them.
 */
static bool debugEnabled()
{
   static const bool enabled = (getenv("PSCAN_XSERIES_DEBUG") != NULL);
   return enabled;
}

/*
 * Stop the STC3 watchdog timer from touching the outputs.
 *
 * THIS IS WHAT WAS DROPPING THE ANALOG OUTPUT.
 *
 * The DAQ-STC3 has a watchdog that software is expected to ping with
 * alternating 0xFEED/0xF00D writes. Nothing in pscan or in the MHDDK's AI/AO
 * examples pings it, so it runs down and expires - and on expiry it "can
 * generate a pulse that can be exported to I/O resources (such as AO or DO)
 * ... placing the outputs on a safe state" (the DDK's BrdServices.rbm).
 *
 * Observed on a PCIe-6351: ao0 set to +2.5V held its DAC code 0x9fb1 for
 * eight seconds and then became 0x8002, which is the calibrated code for
 * 0.000V. Not drift, not a dead output stage - a deliberate hardware write
 * of a safe value, on a timer.
 *
 * It took so long to find because every AO example in the DDK finishes well
 * inside the timeout: aoex1 writes 20 updates 10ms apart and exits in 0.2s,
 * so it never sees this. Only holding an output exposes it, which is exactly
 * what a scanning mirror does for minutes at a time.
 *
 * The sequence is NI's own, from dioex6.cpp, whose comment also warns that
 * "outstanding Watchdog Timer expirations prevent hardware programming" -
 * so an expiry does not only drop the outputs, it can make later register
 * programming silently not take.
 *
 * Losing the watchdog costs pscan nothing. Its purpose is to park the
 * hardware if software stops responding, and vfio-pci already does exactly
 * that: it resets the card when the last file descriptor closes, so a crash
 * or a kill drops the outputs anyway. Keeping it would mean pinging the card
 * every few seconds from Python throughout a scan, for a guarantee we
 * already have.
 */
static void quiet_watchdog(xseries_session* s, nMDBG::tStatus2& status)
{
   if (status.isFatal()) return;

   // dioex6.cpp's constants.
   const u16 kWdtPause = 0x1234;   // stop counting
   const u16 kWdtReset = 0xACED;   // acknowledge an expiry and restart clean

   if (debugEnabled())
   {
      const u32 state =
         s->device->BrdServices.WatchdogStatusRegister.readWatchdogSM_State(&status);
      const u32 expired =
         s->device->BrdServices.WatchdogStatusRegister.readWatchdogExpiredCnt(&status);
      // State 5 is ExpiredPulse and 6 is Expired, per the RBM's
      // tBrdSrv_WatchdogTimerStateMachineSt_t.
      printf("[xseries] watchdog before: state=%u%s, expired %u time(s)\n",
             state, (state == 5 || state == 6) ? " (EXPIRED)" : "", expired);
   }

   // No expiry trigger from either source, so even a timer that somehow runs
   // cannot reach the outputs.
   s->device->BrdServices.WatchdogConfiguration.writeWatchdogIntTrigEn(
      kFalse, &status);
   s->device->BrdServices.WatchdogConfiguration.writeWatchdogExtTrigEn(
      kFalse, &status);

   // Acknowledge anything already latched BEFORE pausing: an outstanding
   // expiration blocks programming, and 0xACED is what clears it.
   s->device->BrdServices.WatchdogControl.writeRegister(kWdtReset, &status);
   s->device->BrdServices.WatchdogControl.writeRegister(kWdtPause, &status);

   if (debugEnabled())
   {
      const u32 state =
         s->device->BrdServices.WatchdogStatusRegister.readWatchdogSM_State(&status);
      printf("[xseries] watchdog after:  state=%u (paused, triggers off)\n",
             state);
   }
}

/*
 * Compare a DAC shadow reading against the code that was written.
 *
 * The two registers do NOT use the same encoding, and assuming they did cost
 * a round trip of hardware debugging. AO_Direct_Data takes the DDK scaler's
 * output, which is two's complement (0x0000 is 0V). AO_DacShadow reports
 * OFFSET BINARY (0x8000 is 0V), so a correct, settled DAC reads back as the
 * code written XOR 0x8000.
 *
 * Observed: wrote 0x1fb1 for +2.5V, shadow read 0x9fb1 - differing in bit 15
 * alone - and decoding the shadow as offset binary gives +2.476V, which is
 * what the meter showed. Three agreeing numbers.
 *
 * So bit 15 flipped is the HEALTHY case. Reporting it as a mismatch sends
 * the reader after a digital fault that is not there.
 */
static const char* shadow_verdict(u32 shadow, u16 wrote)
{
   if (shadow == static_cast<u32>(wrote ^ 0x8000))
      return "OK (offset binary; the DAC holds the value written)";
   if (shadow == wrote)
      return "OK, but same encoding as written - expected bit 15 flipped";
   return "MISMATCH - the DAC is not holding what was written";
}

// Map pscan's terminal names onto nAI::tAI_Config_Channel_Type_t. The X
// Series AI enum offers Differential, NRSE and RSE; there is no
// pseudodifferential entry, so that request is refused rather than silently
// substituted.
static bool map_terminal(u32 terminal, nAI::tAI_Config_Channel_Type_t* out)
{
   switch (terminal)
   {
      case 0: *out = nAI::kRSE;          return true;
      case 1: *out = nAI::kNRSE;         return true;
      case 2: *out = nAI::kDifferential; return true;
      default: return false;
   }
}

/*
 * Everything below is the public API, and must be exported.
 *
 * The library is built with -fvisibility=hidden so that NI's several
 * thousand C++ symbols stay internal and cannot clash with anything else in
 * the process. That default hides these functions too: without the push
 * below the library links cleanly, loads cleanly, and then ctypes reports
 *
 *     undefined symbol: xseries_open
 *
 * A new entry point added inside this block is exported automatically, which
 * is what we want here - the block IS the API.
 */
#pragma GCC visibility push(default)

extern "C" {

/*
 * Open one card.
 *
 * `resource` is the DDK's VISA-style board location, "PXI<bus>::<dev>::INSTR"
 * - for a card at PCI 82:00.0 that is "PXI130::0::INSTR", since the DDK wants
 * the bus and device numbers in decimal.
 */
/*
 * Defined in build_id.cpp, which is recompiled on every link so the answer
 * describes THIS library rather than the last time this file changed. It
 * lived here and reported a stale timestamp while the library was current,
 * which is the one thing it must never do.
 */
const char* xseries_build_id(void);

void* xseries_open(const char* resource)
{
   if (debugEnabled())
      printf("[xseries] shim built %s\n", xseries_build_id());

   xseries_session* s = new xseries_session();
   memset(s, 0, sizeof(*s));
   // 0 is a legitimate DAC code, so "nothing written yet" cannot be 0.
   for (size_t i = 0; i < sizeof(s->aoLastCode) / sizeof(s->aoLastCode[0]); ++i)
      s->aoLastCode[i] = -1;

   s->bus = acquireBoard(const_cast<tChar*>(resource));
   if (s->bus == NULL)
   {
      // acquireBoard() has already printed the specific reason, with errno
      // where there was one. Repeating a guess here - this message used to
      // assert it was a permissions problem - only buries the real answer.
      set_err(s, "acquireBoard('%s') failed; see the reason printed above",
              resource);
      return s;
   }

   s->bar0 = s->bus->createAddressSpace(kPCI_BAR0);

   nMDBG::tStatus2 status;
   s->device = new tXSeries(s->bar0, &status);
   if (status.isFatal())
   {
      set_err(s, "tXSeries construction failed (%d)", status.statusCode);
      return s;
   }

   s->info = nNISTC3::getDeviceInfo(*s->device, status);
   if (status.isFatal() || s->info == NULL)
   {
      set_err(s, "cannot identify device (%d) - is this an X Series board?",
              status.statusCode);
      return s;
   }

   quiet_watchdog(s, status);
   if (status.isFatal())
   {
      set_err(s, "could not quiet the watchdog timer (%d)", status.statusCode);
      return s;
   }

   if (s->info->isSimultaneous)
   {
      nNISTC3::initializeSimultaneousXSeries(*s->device, status);
   }

   // Reading the calibration EEPROM is slow, so it happens once here rather
   // than per trace. Both scalers need it.
   s->eeprom = new nNISTC3::eepromHelper(
      *s->device, s->info->isSimultaneous,
      s->info->numberOfADCs, s->info->numberOfDACs, status);
   if (status.isFatal())
   {
      set_err(s, "could not read the calibration EEPROM (%d)",
              status.statusCode);
      return s;
   }

   s->ao = new nNISTC3::aoHelper(
      s->device->AO, s->device->AO.AO_Timer, status);
   if (status.isFatal())
   {
      set_err(s, "could not create the AO helper (%d)", status.statusCode);
      return s;
   }

   /*
    * Reset the AO subsystem once, here, before anything is programmed.
    *
    * NI's aoex1 does this and an earlier version of this file did not, which
    * is why aoex1 drove a meter on this rig and pscan did not. Constructing
    * an aoHelper does NOT reset anything - the constructor only stores
    * references - so without this the subsystem is left in whatever state
    * the VFIO function-level reset happened to produce.
    *
    * What it does: clears AO_Trigger_Select_Register, seeds every
    * AO_Config_Bank, and - the part that matters - resets the AO OUT TIMER.
    * A stale out timer updates the DACs on its own schedule from whatever is
    * in the AO FIFO, so an output that was written once creeps and steps
    * instead of holding, which is exactly what the bench showed.
    *
    * Once at open, deliberately. Doing it per write would drop the output
    * through 0V at every step of a scan.
    */
   s->ao->reset(status);
   if (status.isFatal())
   {
      set_err(s, "could not reset the AO subsystem (%d)", status.statusCode);
      return s;
   }

   /*
    * Define aoHelper's reset-to-zero state rather than leaving it to chance.
    *
    * _resetToZero is not in aoHelper's constructor initialiser list - an NI
    * bug - so until this call it holds whatever was on the stack. An empty
    * vector with kFalse says plainly: on destruction, leave the outputs
    * where they are. pscan never deletes the helper, so this only matters if
    * that ever changes, which is precisely when an uninitialised bool would
    * be hardest to find.
    */
   const std::vector<i16> noZeroVolts;
   s->ao->setZeroVolts(noZeroVolts, kFalse, status);

   s->err[0] = '\0';
   return s;
}

const char* xseries_last_error(void* handle)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL) return "no session";
   return s->err;
}

int xseries_is_open(void* handle)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   return (s != NULL && s->bus != NULL && s->info != NULL && s->err[0] == '\0');
}

/*
 * Release the card.
 *
 * The analog outputs are deliberately not driven to zero here: a scan ending
 * must not slam a mirror or a piezo back through its range. Note that
 * vfio-pci zeroes them anyway when the last file descriptor closes - see the
 * note below - so this only governs what happens while the session is still
 * up.
 */
void xseries_close(void* handle)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL) return;

   /*
    * s->ao is deliberately NOT deleted, and this is not an oversight.
    *
    * aoHelper's destructor calls reset() unconditionally, which drops the
    * analog outputs to zero. Observed on the bench: NI's own aoex1 leaves
    * AO0 reading 0 V on a meter once it exits. For pscan that is the wrong
    * behaviour - a scan ending must not slam a mirror or a piezo back
    * through its range, which is why the Phidget path has always left its
    * output energized.
    *
    * (The destructor's other branch, writing _zeroVolts, cannot reach us
    * either: xseries_open() calls setZeroVolts() with an empty vector and
    * kFalse, so there is nothing to write and the flag says not to. That
    * call also defines _resetToZero, which NI leaves out of aoHelper's
    * constructor initialiser list.)
    *
    * BUT NOTE: this does not make the output survive the process. vfio-pci
    * resets the device when its last file descriptor closes - deliberately,
    * so a userspace driver cannot leave hardware running unattended - and
    * that reset zeroes the DACs regardless of what we do here. Skipping
    * aoHelper's reset only avoids zeroing the output EARLIER, while the
    * session is still open and something might still use it.
    *
    * Holding a voltage therefore means holding the card open. A scan does
    * that for its whole duration; 'pydaq set --hold' does it explicitly.
    *
    * One small object per process, released when the process exits.
    */
   s->ao = NULL;

   delete s->eeprom;
   delete s->device;
   if (s->bus != NULL) releaseBoard(s->bus);
   delete s;
}

int xseries_info(void* handle, xseries_device_info* out)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->info == NULL || out == NULL) return -1;

   memset(out, 0, sizeof(*out));
   out->product_id = s->info->productID;
   snprintf(out->name, sizeof(out->name), "%s", s->info->deviceName);
   out->is_simultaneous = s->info->isSimultaneous ? 1 : 0;
   out->n_ai_channels = s->info->numberOfAIChannels;
   out->n_ao_channels = s->info->numberOfDACs;
   out->n_adcs = s->info->numberOfADCs;
   out->n_dacs = s->info->numberOfDACs;

   nMDBG::tStatus2 status;
   nNISTC3::streamHelper streamHelper(
      s->device->AIStreamCircuit, s->device->CHInCh, status);
   // The FIFO is configured two bytes per sample below, so its byte size is
   // twice the number of samples a finite trace may hold.
   out->ai_fifo_samples = streamHelper.getFifoSize() / sizeof(i16);

   return status.isFatal() ? -1 : 0;
}

/*
 * Read the card's INTERNAL temperature sensor.
 *
 * This is a diagnostic, and a deliberately faithful transcription of the
 * DDK's boardBringup.cpp getCurrentTemp() - same periods, same sample count,
 * same order, nothing of mine added.
 *
 * Its value is that the signal is internal to the card. No terminal block,
 * no umbilical, no wiring: if the AI chain works at all, this returns a
 * plausible room-ish temperature. So it separates two very different
 * failures that look identical from outside:
 *
 *   plausible temperature -> the ADC, timing and FIFO are all fine, and the
 *                            problem is in selecting an EXTERNAL channel
 *   zero, like the external reads -> the ADC is not converting at all, and
 *                            the fault is below channel selection
 *
 * `temperature_c` and `mean_volts` may each be NULL. Returns 0 on success.
 */
int xseries_selftest(void* handle, double* temperature_c, double* mean_volts)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->device == NULL || s->info == NULL) return -1;

   nMDBG::tStatus2 status;

   if (s->info->isSimultaneous)
   {
      // Simultaneous devices have a dedicated sensor rather than reading it
      // through AI, so this tells us nothing about the AI path. The 6351 is
      // not one of these.
      set_err(s, "the temperature self-test only exercises AI on "
                 "multiplexed devices; this one is simultaneous");
      return -1;
   }

   const u32 numSamps = 256;
   std::vector<i16> rawData(numSamps, 0);
   std::vector<f32> scaledData(numSamps, 0);

   nNISTC3::aiHelper aiHelper(*s->device, s->info->isSimultaneous, status);
   aiHelper.reset(status);

   s->device->AI.AI_Timer.Reset_Register.writeConfiguration_Start(kTrue, &status);
   aiHelper.programExternalGate(nAI::kGate_Disabled,
                                nAI::kActive_High_Or_Rising_Edge, status);
   aiHelper.programStart1(nAI::kStart1_SW_Pulse,
                          nAI::kActive_High_Or_Rising_Edge, kTrue, status);
   aiHelper.programStart(nAI::kStartCnv_InternalTiming,
                         nAI::kActive_High_Or_Rising_Edge, kTrue, status);
   aiHelper.programConvert(nAI::kStartCnv_InternalTiming,
                           nAI::kActive_Low_Or_Falling_Edge, status);

   nNISTC3::inTimerParams timingConfig;
   timingConfig.setAcqLevelTimingMode(nNISTC3::kInTimerPostTrigger, status);
   timingConfig.setUseSICounter(kTrue, status);
   timingConfig.setSamplePeriod(500, status);
   timingConfig.setSampleDelay(2, status);
   timingConfig.setNumberOfSamples(numSamps, status);
   timingConfig.setUseSI2Counter(kTrue, status);
   timingConfig.setConvertPeriod(500, status);
   timingConfig.setConvertDelay(2, status);
   aiHelper.getInTimerHelper(status).programTiming(timingConfig, status);

   aiHelper.programFIFOWidth(nAI::kTwoByteFifo, status);
   s->device->AI.AI_Timer.Reset_Register.writeConfiguration_End(kTrue, &status);

   aiHelper.getInTimerHelper(status).clearConfigurationMemory(status);
   std::vector<nNISTC3::aiHelper::tChannelConfiguration> chanConfig(
      1, nNISTC3::aiHelper::tChannelConfiguration());
   chanConfig[0].isLastChannel = kTrue;
   chanConfig[0].enableDither = nAI::kEnabled;
   chanConfig[0].gain = s->info->getAI_Gain(nNISTC3::kInput_10V, status);
   chanConfig[0].range = nNISTC3::kInput_10V;
   chanConfig[0].channelType = nAI::kInternal;   // not an external pin
   chanConfig[0].bank = nAI::kBank0;
   chanConfig[0].channel = 4;                    // the temperature sensor
   aiHelper.programChannel(chanConfig[0], status);
   aiHelper.getInTimerHelper(status).primeConfigFifo(status);

   aiHelper.getInTimerHelper(status).armTiming(timingConfig, status);
   aiHelper.getInTimerHelper(status).strobeStart1(status);

   // Bounded, unlike the DDK's bare spin loops - a wedged card should not
   // hang the caller forever.
   const double timeout = 5.0;
   clock_t begin = clock();
   while (!s->device->AI.AI_Timer.Status_1_Register.readSC_TC_St(&status))
   {
      if (static_cast<double>(clock() - begin) / CLOCKS_PER_SEC > timeout)
      {
         set_err(s, "self-test: scan did not complete within %.0fs", timeout);
         return -1;
      }
   }

   begin = clock();
   while (s->device->AI.AI_Data_FIFO_Status_Register.readRegister(&status)
          != numSamps)
   {
      if (static_cast<double>(clock() - begin) / CLOCKS_PER_SEC > timeout)
      {
         set_err(s, "self-test: FIFO never reached %u samples", numSamps);
         return -1;
      }
   }

   for (u32 i = 0; i < numSamps; ++i)
   {
      rawData[i] = s->device->AI.AI_FIFO_Data_Register16.readRegister();
   }

   if (debugEnabled())
   {
      printf("[xseries] selftest raw codes:");
      for (u32 i = 0; i < 8; ++i)
         printf(" 0x%04x", static_cast<u16>(rawData[i]));
      printf("\n");
   }

   nNISTC3::nAIDataHelper::scaleData(rawData, numSamps, scaledData, numSamps,
                                     chanConfig, *s->eeprom, *s->info);

   double mean = 0.0;
   for (u32 i = 0; i < numSamps; ++i) mean += scaledData[i];
   mean /= numSamps;

   aiHelper.reset(status);

   if (mean_volts != NULL) *mean_volts = mean;
   if (temperature_c != NULL) *temperature_c = ((-1.0 * mean) - 0.5) * 100.0;

   return 0;
}

/*
 * Acquire one finite hardware-timed trace from one AI channel.
 *
 * `range_idx` indexes nNISTC3::tInputRange: 0=+/-10V, 1=+/-5V, 2=+/-2V,
 * 3=+/-1V, 4=+/-500mV, 5=+/-200mV, 6=+/-100mV.
 * `terminal` is 0=RSE, 1=NRSE, 2=differential.
 * `out` receives n_samples volts and must be allocated by the caller.
 *
 * Returns 0 on success, negative on failure with xseries_last_error() set.
 */
int xseries_read_ai(void* handle,
                    u32 channel,
                    u32 range_idx,
                    u32 n_samples,
                    double rate_hz,
                    u32 terminal,
                    double* out)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->device == NULL || s->info == NULL) return -1;
   if (out == NULL || n_samples == 0) { set_err(s, "bad arguments"); return -1; }

   nMDBG::tStatus2 status;
   const nNISTC3::tInputRange range =
      static_cast<nNISTC3::tInputRange>(range_idx);

   nAI::tAI_Config_Channel_Type_t termConfig;
   if (!map_terminal(terminal, &termConfig))
   {
      set_err(s, "terminal %u is not supported on X Series AI (use RSE, "
                 "NRSE or differential)", terminal);
      return -1;
   }

   if (channel >= s->info->numberOfAIChannels)
   {
      set_err(s, "ai%u does not exist; this device has %u AI channels",
              channel, s->info->numberOfAIChannels);
      return -1;
   }

   const u16 gain = s->info->getAI_Gain(range, status);
   if (status.isFatal())
   {
      set_err(s, "input range %u is not supported by this device", range_idx);
      return -1;
   }

   // The DDK requires at least two scans for a hardware-timed measurement.
   if (n_samples < 2)
   {
      set_err(s, "a hardware-timed trace needs at least 2 samples");
      return -1;
   }

   nNISTC3::aiHelper aiHelper(*s->device, s->info->isSimultaneous, status);
   nNISTC3::streamHelper streamHelper(
      s->device->AIStreamCircuit, s->device->CHInCh, status);

   // The whole trace has to fit in the FIFO, because without DMA there is no
   // way to drain it while the acquisition runs.
   const u32 fifoBytes = streamHelper.getFifoSize();
   if (n_samples * sizeof(i16) > fifoBytes)
   {
      set_err(s, "%u samples exceed the AI FIFO (%u samples). Without DMA a "
                 "finite trace must fit in the FIFO.",
              n_samples, static_cast<u32>(fifoBytes / sizeof(i16)));
      return -1;
   }

   // Sample period in timebase ticks. One channel, so the only constraint is
   // that there is room for its single convert.
   if (rate_hz <= 0.0) { set_err(s, "sample rate must be positive"); return -1; }
   u32 samplePeriod = static_cast<u32>(llround(kTimebaseHz / rate_hz));
   if (samplePeriod < 1) samplePeriod = 1;

   if (!s->info->isSimultaneous && samplePeriod < kConvertPeriod)
   {
      set_err(s, "sample rate %.0f Hz is too fast for this multiplexed "
                 "device: the convert clock needs %u ticks, so the ceiling "
                 "is about %.0f Hz",
              rate_hz, kConvertPeriod, kTimebaseHz / kConvertPeriod);
      return -1;
   }

   std::vector<nNISTC3::aiHelper::tChannelConfiguration> chanConfig(
      1, nNISTC3::aiHelper::tChannelConfiguration());
   nNISTC3::inTimerParams timingConfig;

   // All PFI lines inputs: nothing here drives them, and leaving one an
   // output could fight whatever is wired to it.
   s->device->Triggers.PFI_Direction_Register.writeRegister(0x0, &status);

   aiHelper.reset(status);

   // --- timing ---------------------------------------------------------
   s->device->AI.AI_Timer.Reset_Register.writeConfiguration_Start(kTrue, &status);

   aiHelper.programExternalGate(
      nAI::kGate_Disabled, nAI::kActive_High_Or_Rising_Edge, status);

   // Software start trigger, so a trace begins when pscan asks rather than
   // on an external edge.
   aiHelper.programStart1(
      nAI::kStart1_SW_Pulse, nAI::kActive_High_Or_Rising_Edge, kTrue, status);

   aiHelper.programStart(
      nAI::kStartCnv_InternalTiming, nAI::kActive_High_Or_Rising_Edge,
      kTrue, status);

   if (s->info->isSimultaneous)
   {
      aiHelper.programConvert(
         nAI::kStartCnv_InternalTiming, nAI::kActive_High_Or_Rising_Edge,
         status);
   }
   else
   {
      aiHelper.programConvert(
         nAI::kStartCnv_InternalTiming, nAI::kActive_Low_Or_Falling_Edge,
         status);
   }

   timingConfig.setAcqLevelTimingMode(nNISTC3::kInTimerPostTrigger, status);
   timingConfig.setUseSICounter(kTrue, status);
   timingConfig.setSamplePeriod(samplePeriod, status);
   timingConfig.setSampleDelay(kSampleDelay, status);
   timingConfig.setNumberOfSamples(n_samples, status);
   timingConfig.setRetriggerRecord(kFalse, status);  // one trace per request
   if (!s->info->isSimultaneous)
   {
      timingConfig.setUseSI2Counter(kTrue, status);
      timingConfig.setConvertPeriod(kConvertPeriod, status);
      timingConfig.setConvertDelay(kConvertDelay, status);
   }

   aiHelper.getInTimerHelper(status).programTiming(timingConfig, status);
   aiHelper.programFIFOWidth(nAI::kTwoByteFifo, status);

   /*
    * Where writeConfiguration_End goes, and why it is switchable.
    *
    * NI's own examples disagree. aiex2 programs the channel INSIDE the
    * configuration window and closes it afterwards. boardBringup's
    * getCurrentTemp() - a single-channel read, closer to this one, and known
    * to return a sensible temperature - closes the window FIRST and programs
    * the channel after.
    *
    * Following aiex2 produced a trace of exactly 0x0000 on every sample,
    * with the FIFO filling and draining correctly and dither enabled. That
    * is what an unconfigured input mux reads as, which points at the channel
    * configuration not taking effect.
    *
    * getCurrentTemp's order is therefore the default. PSCAN_XSERIES_CFG_IN_WINDOW=1
    * restores aiex2's, so both can be tried from one build. This switch is a
    * diagnostic: once the hardware has settled the question, the losing
    * branch should be deleted.
    */
   const bool configInsideWindow =
      (getenv("PSCAN_XSERIES_CFG_IN_WINDOW") != NULL);

   if (!configInsideWindow)
   {
      s->device->AI.AI_Timer.Reset_Register.writeConfiguration_End(kTrue, &status);
   }

   // --- channel --------------------------------------------------------
   aiHelper.getInTimerHelper(status).clearConfigurationMemory(status);

   chanConfig[0].isLastChannel = kTrue;
   chanConfig[0].enableDither = nAI::kEnabled;   // helps ADC accuracy
   chanConfig[0].gain = gain;
   chanConfig[0].channelType = termConfig;
   chanConfig[0].bank = (channel < 16) ? nAI::kBank0 : nAI::kBank1;
   chanConfig[0].channel = static_cast<u16>(channel % 16);
   chanConfig[0].range = range;
   aiHelper.programChannel(chanConfig[0], status);

   aiHelper.getInTimerHelper(status).primeConfigFifo(status);

   if (configInsideWindow)
   {
      s->device->AI.AI_Timer.Reset_Register.writeConfiguration_End(kTrue, &status);
   }

   if (debugEnabled())
   {
      printf("[xseries] config order: channel programmed %s the timing "
             "window (%s)\n",
             configInsideWindow ? "INSIDE" : "AFTER",
             configInsideWindow ? "aiex2" : "getCurrentTemp");
      printf("[xseries] chan=%u bank=%d type=%d gain=%u dither=on\n",
             channel, chanConfig[0].bank, chanConfig[0].channelType, gain);
   }

   if (status.isFatal())
   {
      set_err(s, "AI configuration failed (%d)", status.statusCode);
      return -1;
   }

   // --- run ------------------------------------------------------------
   if (debugEnabled())
   {
      printf("[xseries] fifo=%u bytes, samplePeriod=%u, convertPeriod=%u, "
             "n=%u, gain=%u, range=%u, term=%u, rate=%.0f\n",
             fifoBytes, samplePeriod, kConvertPeriod, n_samples,
             gain, range_idx, terminal, rate_hz);
      printf("[xseries] before arm: SC_TC=%d armed=%d fifoStatus=%u\n",
             s->device->AI.AI_Timer.Status_1_Register.readSC_TC_St(&status),
             s->device->AI.AI_Timer.Status_1_Register.readSC_Armed_St(&status),
             s->device->AI.AI_Data_FIFO_Status_Register.readRegister(&status));
   }

   aiHelper.getInTimerHelper(status).armTiming(timingConfig, status);
   aiHelper.getInTimerHelper(status).strobeStart1(status);

   if (debugEnabled())
   {
      printf("[xseries] after strobe: SC_TC=%d armed=%d fifoStatus=%u\n",
             s->device->AI.AI_Timer.Status_1_Register.readSC_TC_St(&status),
             s->device->AI.AI_Timer.Status_1_Register.readSC_Armed_St(&status),
             s->device->AI.AI_Data_FIFO_Status_Register.readRegister(&status));
   }

   // Wait for the scan counter to reach terminal count, i.e. all n_samples
   // clocked. The physical time is n/rate, so the timeout allows for that
   // plus slack.
   const double physical = static_cast<double>(n_samples) / rate_hz;
   const double timeout = (physical * 2.0) + kRlpTimeoutS;

   clock_t begin = clock();
   while (!s->device->AI.AI_Timer.Status_1_Register.readSC_TC_St(&status))
   {
      if (static_cast<double>(clock() - begin) / CLOCKS_PER_SEC > timeout)
      {
         set_err(s, "AI scan did not complete within %.2fs", timeout);
         s->device->AI.AI_Timer.Command_Register.writeDisarm(kTrue, &status);
         return -1;
      }
   }

   // Then wait for the samples to actually be in the FIFO.
   u32 available = 0;
   begin = clock();
   for (;;)
   {
      available = s->device->AI.AI_Data_FIFO_Status_Register.readRegister(&status);
      if (available >= n_samples) break;

      if (static_cast<double>(clock() - begin) / CLOCKS_PER_SEC > timeout)
      {
         set_err(s, "AI FIFO holds %u of %u samples after %.2fs",
                 available, n_samples, timeout);
         s->device->AI.AI_Timer.Command_Register.writeDisarm(kTrue, &status);
         return -1;
      }
   }

   if (debugEnabled())
   {
      printf("[xseries] FIFO reports %u samples available (wanted %u)\n",
             available, n_samples);
   }

   std::vector<i16> rawData(n_samples, 0);
   for (u32 i = 0; i < n_samples; ++i)
   {
      rawData[i] = s->device->AI.AI_FIFO_Data_Register16.readRegister();
   }

   if (debugEnabled())
   {
      printf("[xseries] first 8 raw codes:");
      for (u32 i = 0; i < 8 && i < n_samples; ++i)
         printf(" 0x%04x", static_cast<u16>(rawData[i]));
      printf("\n");

      // How many distinct values came back. One, from thousands of samples,
      // means the FIFO is not advancing rather than that the signal is
      // quiet - which is the difference between a wiring question and a
      // driver bug.
      u32 distinct = 0;
      for (u32 i = 0; i < n_samples; ++i)
      {
         bool seen = false;
         for (u32 j = 0; j < i && !seen; ++j) if (rawData[j] == rawData[i]) seen = true;
         if (!seen && ++distinct >= 16) break;
      }
      printf("[xseries] distinct raw values in the trace: %s%u\n",
             distinct >= 16 ? ">=" : "", distinct);
      printf("[xseries] FIFO status after draining: %u\n",
             s->device->AI.AI_Data_FIFO_Status_Register.readRegister(&status));
   }

   // Acknowledge the trigger and terminal-count events, or the next trace
   // reports SC_TC twice without acknowledgement.
   s->device->AI.AI_Timer.Interrupt1_Register.writeSTART1_Interrupt_Ack(kTrue, &status);
   s->device->AI.AI_Timer.Interrupt1_Register.writeSC_TC_Interrupt_Ack(kTrue, &status);

   // --- check for faults before trusting the data -----------------------
   s->device->AI.AI_Timer.Status_1_Register.refresh(&status);
   const bool scanOverrun = s->device->AI.AI_Timer.Status_1_Register.getScanOverrun_St(&status) != 0;
   const bool adcOverrun = s->device->AI.AI_Timer.Status_1_Register.getOverrun_St(&status) != 0;
   const bool fifoOverflow = s->info->isSimultaneous
      ? (s->device->SimultaneousControl.InterruptStatus.readAiFifoOverflowInterruptCondition(&status) != 0)
      : (s->device->AI.AI_Timer.Status_1_Register.getOverflow_St(&status) != 0);

   s->device->AI.AI_Timer.Command_Register.writeDisarm(kTrue, &status);

   if (scanOverrun || adcOverrun || fifoOverflow)
   {
      set_err(s, "AI fault: %s%s%s- the trace is not trustworthy",
              scanOverrun ? "sample clock overrun " : "",
              adcOverrun ? "ADC overrun " : "",
              fifoOverflow ? "FIFO overflow " : "");
      return -1;
   }

   // --- scale ----------------------------------------------------------
   // Through the DDK's own scaler, so the EEPROM calibration constants are
   // applied. Raw codes scaled by hand are off by around a percent.
   std::vector<f32> scaled(n_samples, 0.0f);
   nNISTC3::nAIDataHelper::scaleData(
      rawData, n_samples, scaled, n_samples,
      chanConfig, *s->eeprom, *s->info);

   for (u32 i = 0; i < n_samples; ++i) out[i] = static_cast<double>(scaled[i]);

   return 0;
}

/*
 * Hold one AO channel at a voltage.
 *
 * `range_idx` indexes nNISTC3::tOutputRange: 0=+/-10V, 1=+/-5V.
 *
 * The channel's config bank is programmed once per (channel, range) and then
 * left alone, so repeated writes during a scan are a single DAC register
 * write. Reprogramming per write would run aoHelper::reset() each time and
 * glitch the output through zero at every scan step.
 */
int xseries_write_ao(void* handle, u32 channel, u32 range_idx, double volts)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->device == NULL || s->info == NULL || s->ao == NULL)
      return -1;

   if (channel >= s->info->numberOfDACs)
   {
      set_err(s, "ao%u does not exist; this device has %u DACs",
              channel, s->info->numberOfDACs);
      return -1;
   }
   // numberOfDACs above is the real limit, and comes from the device table.
   // This is the backstop: the DDK's register banks are fixed-size arrays
   // (AO_Config_Bank and AO_DacShadow hold 8 each), so a wrong table entry
   // for some future card would index past them and corrupt memory rather
   // than be refused. Taken from the arrays themselves so it cannot drift.
   const size_t kBanks = sizeof(s->device->AO.AO_Config_Bank) /
                         sizeof(s->device->AO.AO_Config_Bank[0]);
   const size_t kShadows = sizeof(s->device->AO.AO_DacShadow) /
                           sizeof(s->device->AO.AO_DacShadow[0]);
   const size_t kTracked = sizeof(s->aoConfigured) / sizeof(s->aoConfigured[0]);
   if (channel >= kBanks || channel >= kShadows || channel >= kTracked)
   {
      set_err(s, "ao%u is beyond this chip's register banks", channel);
      return -1;
   }

   nMDBG::tStatus2 status;
   const nNISTC3::tOutputRange range =
      static_cast<nNISTC3::tOutputRange>(range_idx);

   const u8 gain = s->info->getAO_Gain(range, status);
   if (status.isFatal())
   {
      set_err(s, "output range %u is not supported by this device", range_idx);
      return -1;
   }

   std::vector<nNISTC3::aoHelper::tChannelConfiguration> chanConfig(
      1, nNISTC3::aoHelper::tChannelConfiguration());
   // aoHelper's struct takes a u32 channel and a u8 gain, unlike aiHelper's
   // u16/u16 - hence no cast here and no mismatch with getAO_Gain()'s u8.
   chanConfig[0].channel = channel;
   chanConfig[0].gain = gain;
   chanConfig[0].updateMode = nAO::kImmediate;
   chanConfig[0].range = range;

   if (!s->aoConfigured[channel] || s->aoConfiguredRange[channel] != range_idx)
   {
      s->device->AO.AO_Timer.Reset_Register.writeConfiguration_Start(kTrue, &status);
      s->ao->programConfigBank(chanConfig[0], status);
      s->device->AO.AO_Timer.Reset_Register.writeConfiguration_End(kTrue, &status);

      if (status.isFatal())
      {
         set_err(s, "AO configuration failed (%d)", status.statusCode);
         return -1;
      }
      s->aoConfigured[channel] = true;
      s->aoConfiguredRange[channel] = range_idx;
   }

   // Volts to DAC code through the DDK's scaler, so calibration applies.
   //
   // Note the signature differs from the AI one in dataHelper.h: it takes
   // f32 (not f64) and does NOT take a tDeviceInfo, because the AO
   // calibration coefficients are per-DAC and come entirely from the EEPROM
   // helper. f32 is ample for a 16-bit DAC.
   std::vector<f32> voltData(1, static_cast<f32>(volts));
   std::vector<i16> codes(1, 0);
   nNISTC3::nAODataHelper::scaleData(
      voltData, 1, codes, 1, chanConfig, *s->eeprom);

   if (debugEnabled())
   {
      // The DAC code, so a disagreement with a meter can be attributed.
      // boardBringup prints the same calibration intervals, e.g. for this
      // card "DAC0: Interval 1, Coefficients 1.842e+00 6.487e+03", so the
      // expected code is checkable by hand: 1.842 + 6487*volts.
      printf("[xseries] AO%u: %+.4f V, range=%u, gain=%u -> DAC code "
             "0x%04x (%d)\n",
             channel, volts, range_idx, gain,
             static_cast<u16>(codes[0]), static_cast<int>(codes[0]));
   }

   s->device->AO.AO_Direct_Data[channel].writeRegister(codes[0], &status);
   if (status.isFatal())
   {
      set_err(s, "AO write failed (%d)", status.statusCode);
      return -1;
   }

   s->aoLastCode[channel] = static_cast<i32>(static_cast<u16>(codes[0]));
   s->aoLastVolts[channel] = volts;

   if (debugEnabled())
   {
      // The shadow lags the write. Read back-to-back it returns 0x0000 on
      // this card and the correct value a moment later, so a single
      // immediate read reports a MISMATCH on a perfectly good write - which
      // it did, and which sent the last round of debugging sideways.
      // Re-read a bounded number of times and say how many it took.
      const u16 wrote = static_cast<u16>(codes[0]);
      const u32 want = static_cast<u32>(wrote ^ 0x8000);
      u32 shadow = 0;
      int tries = 0;
      for (; tries < 100; ++tries)
      {
         shadow = s->device->AO.AO_DacShadow[channel].readRegister(&status)
                  & 0xFFFF;
         if (shadow == want) break;
      }
      printf("[xseries] AO%u: DAC shadow 0x%04x after %d read(s), "
             "wrote 0x%04x -> %s\n",
             channel, shadow, tries + 1, wrote, shadow_verdict(shadow, wrote));
   }

   return 0;
}

/*
 * A spread of key registers, read in one go.
 *
 * For the question that is actually open: something zeroes the analog
 * output about eight seconds after it is set, with the card awake, the
 * watchdog asleep and runtime power management pinned off. Guessing at
 * mechanisms has not worked, so this reads the state AT the moment it
 * happens and lets the card say what changed.
 *
 * What each field settles:
 *
 *   chinch / stc3 signatures  Constant on a healthy card. If these have
 *                             changed, the card was reset underneath us and
 *                             the AO is incidental - a whole different fault.
 *   ao_config_bank            OUR channel configuration: range, update mode,
 *                             bipolar. If this is intact while the DAC is
 *                             zeroed, the event touched only the data
 *                             register. If it has reverted to its 0xBF reset
 *                             value, the AO subsystem itself was reset.
 *   ao_trigger_select         Which trigger drives AO updates. Non-zero when
 *                             we expect zero means something re-armed it.
 *   ao_fifo_status            Data waiting in the AO FIFO. Non-zero would
 *                             mean the out timer has something to push into
 *                             the DAC behind our backs.
 *   wdt_*                     Proof the watchdog really stayed out of it,
 *                             read at the moment rather than at open.
 */
struct xseries_snapshot
{
   unsigned int chinch_signature;
   unsigned int stc3_signature;
   unsigned int ao_dac_shadow;
   unsigned int ao_config_bank;
   unsigned int ao_trigger_select;
   unsigned int ao_fifo_status;
   unsigned int wdt_state;
   unsigned int wdt_expired_count;
};

int xseries_snapshot_registers(void* handle, u32 channel,
                               struct xseries_snapshot* out)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->device == NULL || out == NULL) return -1;

   const size_t kShadows = sizeof(s->device->AO.AO_DacShadow) /
                           sizeof(s->device->AO.AO_DacShadow[0]);
   const size_t kBanks = sizeof(s->device->AO.AO_Config_Bank) /
                         sizeof(s->device->AO.AO_Config_Bank[0]);
   if (channel >= kShadows || channel >= kBanks)
   {
      set_err(s, "ao%u is beyond this chip's register banks", channel);
      return -1;
   }

   nMDBG::tStatus2 status;

   out->chinch_signature =
      s->device->CHInCh.CHInCh_Identification_Register.readRegister(&status);
   out->stc3_signature =
      s->device->BrdServices.Signature_Register.readRegister(&status);
   out->ao_dac_shadow =
      s->device->AO.AO_DacShadow[channel].readRegister(&status) & 0xFFFF;
   out->ao_config_bank =
      s->device->AO.AO_Config_Bank[channel].readRegister(&status);
   out->ao_trigger_select =
      s->device->AO.AO_Trigger_Select_Register.readRegister(&status);
   out->ao_fifo_status =
      s->device->AO.AO_FIFO_Status_Register.readRegister(&status);
   out->wdt_state =
      s->device->BrdServices.WatchdogStatusRegister.readWatchdogSM_State(&status);
   out->wdt_expired_count =
      s->device->BrdServices.WatchdogStatusRegister.readWatchdogExpiredCnt(&status);

   if (status.isFatal())
   {
      set_err(s, "register snapshot failed (%d)", status.statusCode);
      return -1;
   }
   return 0;
}

/*
 * What is the DAC holding right now?
 *
 * AO_DacShadow is "the last value that has been updated on the DACi" (the
 * DDK's AO.rbm), in offset binary and masked to the DAC's 16 bits. Reading
 * it does not disturb the output.
 *
 * This answers a question a meter cannot: whether an output that measures
 * wrong is wrong because the DAC stopped holding the code (digital, ours to
 * fix) or because the analog stage is not reproducing a code it still holds
 * (hardware, not ours). Polled during a hold, it also shows whether the
 * value DRIFTS - something re-arming the out timer and updating the DACs
 * from an empty FIFO would show up here as a changing shadow, while a dead
 * output stage would show a rock-steady one.
 *
 * `out_shadow` receives the raw register, `out_expected` the offset-binary
 * form of the last code written (or -1 if nothing has been written to this
 * channel), and `out_volts` the volts last requested. All three may be NULL.
 *
 * Returns 0 on success, 1 when they disagree, negative on failure.
 */
int xseries_ao_shadow(void* handle, u32 channel,
                      unsigned int* out_shadow,
                      int* out_expected,
                      double* out_volts)
{
   xseries_session* s = static_cast<xseries_session*>(handle);
   if (s == NULL || s->device == NULL || s->info == NULL) return -1;

   const size_t kShadows = sizeof(s->device->AO.AO_DacShadow) /
                           sizeof(s->device->AO.AO_DacShadow[0]);
   const size_t kTracked = sizeof(s->aoLastCode) / sizeof(s->aoLastCode[0]);
   if (channel >= kShadows || channel >= kTracked)
   {
      set_err(s, "ao%u is beyond this chip's register banks", channel);
      return -1;
   }

   nMDBG::tStatus2 status;
   const u32 shadow =
      s->device->AO.AO_DacShadow[channel].readRegister(&status) & 0xFFFF;
   if (status.isFatal())
   {
      set_err(s, "AO shadow read failed (%d)", status.statusCode);
      return -1;
   }

   const i32 last = s->aoLastCode[channel];
   const int expected =
      (last < 0) ? -1 : static_cast<int>((static_cast<u16>(last)) ^ 0x8000);

   if (out_shadow != NULL)   *out_shadow = shadow;
   if (out_expected != NULL) *out_expected = expected;
   if (out_volts != NULL)
      *out_volts = (last < 0) ? 0.0 : s->aoLastVolts[channel];

   if (expected < 0) return 0;
   return (static_cast<int>(shadow) == expected) ? 0 : 1;
}

}  // extern "C"

#pragma GCC visibility pop
