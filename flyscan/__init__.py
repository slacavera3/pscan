"""On-the-fly ASOPS line scanning.

Separate from pscan, which is a step-and-settle orchestrator. The acquisition
model here is different enough that sharing its scope driver or its .dat/.m
output format would help nobody: one oscilloscope acquisition covers a whole
raster line, in Sequence mode, with each segment being one ASOPS delay sweep.
"""
