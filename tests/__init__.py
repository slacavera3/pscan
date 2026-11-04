"""Tests that need no hardware attached.

Every test here runs on a laptop: the DAQ backends are exercised through the
simulated card, and the axis walk through fake axes that only record what they
were told to do. Nothing opens a card, a stage, a scope or a camera.

    python -m unittest discover -s tests -t .

Run from the repository root. Anything that genuinely needs the rig lives in
dev/ and is run by hand.
"""
