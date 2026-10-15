"""
core/action_runner.py
----------------------
Resolves and executes every waypoint action. Replaces the old
custom_code_runner.py, which only handled a special "custom_code" action
type — now there's exactly one mechanism for all actions, because the
mission schema no longer has any built-in, special-cased action types
(no more spray_on/spray_off baked into the schema). Spraying is just a
plugin, same as everything else.

Convention
----------
An ``ActionConfig`` has a ``code`` (e.g. "spray_on", "take_photo") and
``args`` (from the mission XML's ``<Arg>`` elements). The action code maps
directly to a plugin file by naming convention:

    code "spray_on"  ->  plugins/spray_on.py, function run()
    code "take_photo" -> plugins/take_photo.py, function run()

Every plugin must define a callable named ``run`` (sync or async) that
accepts ``drone_context`` as a keyword argument, plus whatever kwargs the
mission's ``<Arg>`` elements provide.

Design goals (unchanged from before)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* Never crash a mission: all plugin errors are caught, logged, and the
  method returns ``None`` — the drone continues to the next waypoint.
* Cache loaded plugins: each action code is loaded once per
  ``ActionRunner`` instance lifetime.
* Async-transparent: async plugins are awaited directly; sync plugins are
  off-loaded to the default executor so they cannot block the event loop.

Usage
-----
>>> runner = ActionRunner()
>>> for action in wp.actions:
...     result = await runner.run(action, drone_context)
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import types
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.mission_plan import ActionConfig

log = logging.getLogger(__name__)

PLUGIN_FUNCTION_NAME = "run"
PLUGINS_DIR = "plugins"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PluginLoadError(Exception):
    """
    Raised when ``ActionRunner.load_plugin()`` cannot locate the plugin
    file for an action code, or the file has no ``run`` function.
    """


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class ActionRunner:
    """
    Loads, caches, and executes the plugin behind every waypoint action.

    Attributes
    ----------
    _cache : Mapping from action code to the resolved callable. Populated
             lazily on first use of that code.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Callable[..., Any]] = {}
        log.debug("ActionRunner initialised.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_plugin(self, action_code: str) -> Callable[..., Any]:
        """
        Resolve *action_code* to ``plugins/<action_code>.py``'s ``run``
        function, loading it from disk the first time and caching it
        after that.

        Raises
        ------
        PluginLoadError
            When the file cannot be found, fails to import, or has no
            ``run`` function.
        """
        if action_code in self._cache:
            log.debug("Plugin cache hit: %s", action_code)
            return self._cache[action_code]

        module_path = str(Path(PLUGINS_DIR) / f"{action_code}.py")
        log.info("Loading plugin for action '%s': %s", action_code, module_path)

        spec = importlib.util.spec_from_file_location(
            name=f"drone_action_{action_code}",
            location=module_path,
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(
                f"No plugin found for action code '{action_code}' "
                f"(expected {module_path}). Every waypoint <Action code=\"...\"> "
                f"must have a matching plugins/<code>.py file."
            )

        module: types.ModuleType = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            raise PluginLoadError(
                f"Error while importing plugin for action '{action_code}' ({module_path}): {exc}"
            ) from exc

        fn = getattr(module, PLUGIN_FUNCTION_NAME, None)
        if fn is None or not callable(fn):
            raise PluginLoadError(
                f"{module_path} has no callable '{PLUGIN_FUNCTION_NAME}()' function "
                f"(required for action code '{action_code}')."
            )

        self._cache[action_code] = fn
        log.debug("Plugin loaded and cached for action '%s'.", action_code)
        return fn

    async def run(
        self,
        action: ActionConfig,
        drone_context: Dict[str, Any],
    ) -> Optional[Any]:
        """
        Execute the plugin behind *action* with *drone_context*.

        Never raises — any failure (missing plugin, plugin exception) is
        logged and the mission continues to the next waypoint.
        """
        try:
            fn = self.load_plugin(action.code)
        except PluginLoadError as exc:
            log.error("Failed to resolve action '%s': %s", action.code, exc)
            return None

        log.info(
            "Running action '%s' args=%s drone_id=%s",
            action.code, action.args, drone_context.get("drone_id", "?"),
        )

        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**action.args, drone_context=drone_context)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: fn(**action.args, drone_context=drone_context),
                )
            log.debug("Action '%s' completed. result=%s", action.code, result)
            return result
        except Exception as exc:
            log.error(
                "Action '%s' raised an exception (mission continues): %s",
                action.code, exc, exc_info=True,
            )
            return None

    def clear_cache(self) -> None:
        self._cache.clear()
        log.debug("Plugin cache cleared.")
