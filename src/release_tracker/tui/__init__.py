"""Textual front end for the tracker — a rendering shell over the same library the CLI uses."""

from release_tracker.tui.app import RdtApp, run

__all__ = ["RdtApp", "run"]
