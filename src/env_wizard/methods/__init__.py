from __future__ import annotations

from typing import Dict

from .base import Check, Ctx, Method, Outcome
from .claude import CcMcp, CcPermissions, ClaudeMd, Template
from .node import FnmNode, FnmScript, NpmPackages, ProjectNodeDeps
from .python import ProjectPythonDeps, PythonPackages, UvPython, UvStandalone, UvTool, UvVenv
from .system import BrewCask, BrewFormula, CheckOnly, HandoffCheck, Winget

REGISTRY: Dict[str, Method] = {m.name: m for m in [
    HandoffCheck(), CheckOnly(), BrewFormula(), BrewCask(), Winget(),
    UvStandalone(), UvPython(), UvVenv(), UvTool(), PythonPackages(), ProjectPythonDeps(),
    FnmScript(), FnmNode(), NpmPackages(), ProjectNodeDeps(),
    CcPermissions(), CcMcp(), ClaudeMd(), Template(),
]}


def get(name: str) -> Method:
    return REGISTRY[name]
