"""Firmware layer (BIOS/UEFI analogue).

The bootstrap package is the smallest, most-trusted code in the system. It verifies
the kernel and launches it, restarting it on crash (the "reboot" of the environment).
It is intentionally tiny and is never modified by the agent.
"""
