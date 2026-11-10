/*
 * build_id.cpp - when this library was built, and from which commit.
 *
 * Its own translation unit, and compiled on EVERY link rather than cached as
 * a .o, because a build id that can be stale is worse than none at all.
 *
 * This lived in xseries_shim.cpp with __DATE__/__TIME__ and reported an old
 * timestamp while the library was genuinely current: make had recompiled
 * osiUserCode_vfio.cpp (which the link rule rebuilds every time) but left
 * xseries_shim.o cached, so the id came from the earlier compile of a file
 * that had not changed. That is exactly the ambiguity the id exists to
 * remove - during a long hardware debugging session, "is the fix in what I
 * just ran?" has to be answerable, and answerable correctly.
 *
 * The Makefile passes PSCAN_XSERIES_BUILD with a UTC timestamp and the short
 * git hash. The fallback matters when someone builds this by hand.
 */

#ifndef PSCAN_XSERIES_BUILD
#define PSCAN_XSERIES_BUILD __DATE__ " " __TIME__ " (no build stamp)"
#endif

#pragma GCC visibility push(default)

extern "C" const char* xseries_build_id(void)
{
   return PSCAN_XSERIES_BUILD;
}

#pragma GCC visibility pop
