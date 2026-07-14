"""MissionOS runtime package.

Runtime contracts are imported from their owning modules. Keeping this package
initializer free of eager compatibility re-exports prevents an adapter import
from loading unrelated robot, simulator, and physical-readiness modules.
"""
