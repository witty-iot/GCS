"""
core/custom_code_runner.py
--------------------------
Dynamic plugin/custom-code executor for the drone_brain swarm system.

At runtime, waypoints with ``action = custom_code`` carry a
``CustomCodeConfig`` specifying a Python module file, a callable name,
and key/value arguments.  ``CustomCodeRunner`` loads these modules on
demand, caches them to avoid repeated disk I/O, and invokes the callable
in an async-safe way regardless of whether the plugin function is
``async def`` or a plain ``def``.

Design goals
~~~~~~~~~~~~
* **Never crash a mission**: all plugin errors are caught, logged, and
  the method returns ``None`` — the drone continues to the next waypoint.
* **Cache loaded plugins**: each ``(module_path, function_name)`` pair
  is loaded once per ``CustomCodeRunner`` instance lifetime.
* **Async-transparent**: async plugins are awaited directly; sync
  plugins are off-loaded to the default executor so they cannot block
  the event loop.

Usage
-----
>>> runner = CustomCodeRunner()
>>> result = await runner.run(wp.custom_code, drone_context)
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import types
from typing import Any, Callable, Dict, Optional, Tuple

from core.mission_plan import CustomCodeConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PluginLoadError(Exception):
    """
    Raised when ``CustomCodeRunner.load_plugin()`` cannot locate the
    requested module file or function name.
    """


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class CustomCodeRunner:
    """
    Loads, caches, and executes dynamic plugin callables.

    Attributes
    ----------
    _cache : Mapping from ``(module_path, function_name)`` to the resolved
             callable.  Populated lazily on first access.
    """

    def __init__(self) -> None:
        """Initialise an empty plugin cache."""
        self._cache: Dict[Tuple[str, str], Callable[..., Any]] = {}
        log.debug("CustomCodeRunner initialised.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_plugin(self, module_path: str, function_name: str) -> Callable[..., Any]:
        """
        Import *module_path* from disk and return its *function_name* attribute.

        The result is cached by ``(module_path, function_name)`` so
        subsequent calls skip the file-system round trip.

        Parameters
        ----------
        module_path   : Path to the Python file (relative to project root
                        or absolute).
        function_name : Name of the callable inside the module.

        Returns
        -------
        Callable[..., Any]
            The resolved plugin function.

        Raises
        ------
        PluginLoadError
            When the file cannot be found, the module fails to import, or
            the function is not present in the module.
        """
        cache_key = (module_path, function_name)
        if cache_key in self._cache:
            log.debug("Plugin cache hit: %s::%s", module_path, function_name)
            return self._cache[cache_key]

        log.info("Loading plugin: %s::%s", module_path, function_name)

        spec = importlib.util.spec_from_file_location(
            name=f"drone_plugin_{module_path.replace('/', '_').replace('.', '_')}",
            location=module_path,
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(
                f"Cannot create module spec for '{module_path}'. "
                "Check that the file exists and is a valid Python file."
            )

        module: types.ModuleType = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            raise PluginLoadError(
                f"Error while importing plugin module '{module_path}': {exc}"
            ) from exc

        fn = getattr(module, function_name, None)
        if fn is None or not callable(fn):
            raise PluginLoadError(
                f"Module '{module_path}' has no callable attribute '{function_name}'."
            )

        self._cache[cache_key] = fn
        log.debug("Plugin loaded and cached: %s::%s", module_path, function_name)
        return fn

    async def run(
        self,
        config: CustomCodeConfig,
        drone_context: Dict[str, Any],
    ) -> Optional[Any]:
        """
        Execute the plugin described by *config* with *drone_context*.

        Execution is async-transparent:
        - Async functions (``async def``) are awaited directly.
        - Sync functions are submitted to the default thread-pool executor
          so they cannot block the event loop.

        Any exception raised by the plugin is caught and logged.  The
        method **always returns** (with ``None`` on failure) so the drone
        mission continues uninterrupted.

        Parameters
        ----------
        config        : Plugin location, callable name, and kwargs.
        drone_context : Runtime context dict injected as the final kwarg.
                        Typically contains ``drone_id``, ``position``,
                        ``battery_pct``, and ``mavsdk_system``.

        Returns
        -------
        Optional[Any]
            Whatever the plugin function returns, or ``None`` on failure.
        """
        try:
            fn = self.load_plugin(config.module_path, config.function_name)
        except PluginLoadError as exc:
            log.error("Failed to load plugin for waypoint action: %s", exc)
            return None

        log.info(
            "Executing plugin %s::%s with args=%s drone_id=%s",
            config.module_path, config.function_name,
            config.args, drone_context.get("drone_id", "?"),
        )

        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**config.args, drone_context=drone_context)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: fn(**config.args, drone_context=drone_context),
                )
            log.debug(
                "Plugin %s::%s completed. result=%s",
                config.module_path, config.function_name, result,
            )
            return result
        except Exception as exc:
            log.error(
                "Plugin %s::%s raised an exception (mission continues): %s",
                config.module_path, config.function_name, exc,
                exc_info=True,
            )
            return None

    def clear_cache(self) -> None:
        """
        Evict all cached plugin modules.

        Useful in tests or when plugins are hot-reloaded during development.
        """
        self._cache.clear()
        log.debug("Plugin cache cleared.")

