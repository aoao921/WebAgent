"""Composable tool packages and a unified dispatch backend.

A ToolPackage bundles one cohesive set of tools (runtime specs, model-facing
definitions and execution).  Domain agents compose packages through
CompositeToolBackend instead of maintaining private registry merges, so
coding primitives and domain tools register through one seam.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from webagent.specs import ToolSpec

from .protocol import ToolDefinition


@runtime_checkable
class ToolPackage(Protocol):
    """One cohesive bundle of tools exposed through the canonical gateway."""

    @property
    def name(self) -> str: ...

    def specs(self) -> dict[str, ToolSpec]:
        """Runtime execution specs keyed by tool name."""
        ...

    def model_definitions(self) -> dict[str, ToolDefinition]:
        """Model-facing definitions keyed by tool name."""
        ...

    async def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]: ...


class _CompositeRegistry:
    """Read-only ``.get(name)`` registry view over composed packages."""

    def __init__(self, specs: dict[str, ToolSpec]) -> None:
        self._specs = specs

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"Unregistered tool: {name}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self):
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


class CompositeToolBackend:
    """Dispatch backend composing several tool packages behind one registry.

    Implements the canonical ``ToolExecutionBackend`` protocol used by
    ``ToolSession``/``ToolGateway``: ``registry.get(name)`` returns the merged
    ToolSpec view and ``execute`` routes to the owning package.
    """

    def __init__(self, packages: list[ToolPackage]) -> None:
        if not packages:
            raise ValueError("CompositeToolBackend requires at least one tool package")
        specs: dict[str, ToolSpec] = {}
        owners: dict[str, ToolPackage] = {}
        package_names: set[str] = set()
        for package in packages:
            package_name = str(package.name)
            if package_name in package_names:
                raise ValueError(f"duplicate tool package name: {package_name}")
            package_names.add(package_name)
            for name, spec in package.specs().items():
                if name in specs:
                    raise ValueError(f"tool name conflict between packages: {name}")
                specs[name] = spec
                owners[name] = package
        self.packages: dict[str, ToolPackage] = {
            str(package.name): package for package in packages
        }
        self.registry = _CompositeRegistry(specs)
        self._owners = owners

    def package_for(self, tool_name: str) -> ToolPackage:
        package = self._owners.get(tool_name)
        if package is None:
            raise KeyError(tool_name)
        return package

    def has(self, tool_name: str) -> bool:
        return tool_name in self._owners

    async def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.package_for(tool_name).execute(tool_name, input_data, state)

    def model_definitions(self) -> dict[str, ToolDefinition]:
        """Merged model-facing definitions across packages in stable order."""
        definitions: dict[str, ToolDefinition] = {}
        for package_name in sorted(self.packages):
            definitions.update(self.packages[package_name].model_definitions())
        return definitions


__all__ = ["CompositeToolBackend", "ToolPackage"]
