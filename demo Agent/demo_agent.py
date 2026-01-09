#!/usr/bin/env python3
"""
Developer Assistant Agent Demo using Microsoft Agent Framework (single-file demo)

README (top instructions)
- Purpose: Demo agent that dynamically registers tools from JSON/YAML configs
  and updates registrations when the configs change (no restart needed).
- Key modules:
    - ToolRegistry: loads and validates JSON/YAML tool configs.
    - AgentManager: registers/unregisters tools via McpToolRegistrationService (or a mock).
    - ConfigWatcher: monitors config files using watchdog and triggers reloads.
- Example configs are created automatically under `configs/` if missing.
- Demo run: starts agent, loads tools, then after a delay programmatically adds
  a DockerHub tool to `configs/tools.json` to show dynamic registration.

Minimal setup:
- Install dependencies:
    pip install -r requirements.txt

- (Optional) If you have the Microsoft Agent Framework SDK installed, install it per
  the SDK docs. The code will try to import `McpToolRegistrationService` from a
  plausible module and fall back to a mock if not available.

Run:
    python demo_agent.py

Notes:
- This is a demo scaffold. Replace the mock registration with the real
  McpToolRegistrationService class if available in your environment's SDK.
- The demo performs real HTTP calls for REST tools (GitHub, StackOverflow).
  Configure API keys if needed or expect limited rate-limited access.

"""

from __future__ import annotations
import os
import sys
import json
import time
import yaml
import threading
import logging
import copy
import requests
from typing import Dict, Any, Callable, Optional, List

# Watchdog imports for file watching
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except Exception as e:
    print("Missing 'watchdog'. Install with: pip install watchdog")
    raise

# Try importing Microsoft Agent Framework registration service.
# The real import path may differ depending on the SDK package name/version.
# If unavailable, a Mock service will be used to keep the demo runnable.
try:
    # Hypothetical / plausible import path - adjust as appropriate if you have the SDK
    from mscopilot.mcp import McpToolRegistrationService  # type: ignore
    HAS_REAL_MCP = True
except Exception:
    # Fallback mock below
    HAS_REAL_MCP = False

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("demo-agent")

# -------------------------
# Mock McpToolRegistrationService (fallback)
# -------------------------
class MockMcpToolRegistrationService:
    """
    Mock replacement for McpToolRegistrationService.
    Stores registrations in-memory and logs calls.
    Exposes:
        - register_tool(name, metadata, handler): registers a tool callable
        - unregister_tool(name): unregisters tool by name
        - list_tools(): returns list of registered tool names
    """
    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register_tool(self, name: str, metadata: Dict[str, Any], handler: Callable[..., Any]):
        if name in self._tools:
            logger.info("Mock: Updating existing registration for tool '%s'", name)
        else:
            logger.info("Mock: Registering tool '%s'", name)
        self._tools[name] = {"metadata": metadata, "handler": handler}

    def unregister_tool(self, name: str):
        if name in self._tools:
            logger.info("Mock: Unregistering tool '%s'", name)
            del self._tools[name]
        else:
            logger.warning("Mock: Attempted to unregister unknown tool '%s'", name)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def invoke(self, name: str, *args, **kwargs):
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered")
        handler = self._tools[name]["handler"]
        return handler(*args, **kwargs)

# Use real or mock
if HAS_REAL_MCP:
    McpService = McpToolRegistrationService  # type: ignore
    logger.info("Using real McpToolRegistrationService from SDK.")
else:
    McpService = MockMcpToolRegistrationService
    logger.info("Microsoft Agent Framework SDK not found; using mock service for demo.")

# -------------------------
# ToolRegistry
# -------------------------
class ToolRegistry:
    """
    Loads tool definitions from JSON and YAML config files.
    Each tool entry should include:
      - name (str)
      - description (str)
      - type (optional): "rest" (default) or "local"
      - endpoint (for REST)
      - method (for REST, GET/POST)
      - params (dict) - default params or schema/description
      - function (for local) - name of local function to wire up
    """

    def __init__(self, config_paths: List[str]):
        self.config_paths = config_paths
        # Keep raw loaded mapping: name -> spec
        self.tools: Dict[str, Dict[str, Any]] = {}

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Load and merge tools from all configured paths.
        Returns a dict mapping tool name -> normalized tool spec.
        """
        loaded: Dict[str, Dict[str, Any]] = {}
        for path in self.config_paths:
            if not os.path.exists(path):
                logger.warning("Config path not found: %s", path)
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                if path.endswith(".json"):
                    parsed = json.loads(text)
                else:
                    parsed = yaml.safe_load(text)
                if isinstance(parsed, dict) and "tools" in parsed:
                    entries = parsed["tools"]
                else:
                    entries = parsed
                if not isinstance(entries, list):
                    logger.error("Config at %s must contain a list of tool entries", path)
                    continue
                for entry in entries:
                    try:
                        norm = self._validate_and_normalize(entry)
                        loaded[norm["name"]] = norm
                    except ValueError as ex:
                        logger.error("Invalid tool entry in %s: %s", path, ex)
            except Exception as ex:
                logger.exception("Failed to read/parse config %s: %s", path, ex)
        self.tools = loaded
        return loaded

    def _validate_and_normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate required fields and normalize the spec.
        Raises ValueError on invalid spec.
        """
        if not isinstance(raw, dict):
            raise ValueError("Tool entry must be an object")
        name = raw.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("Missing or invalid 'name'")
        description = raw.get("description", "")
        ttype = raw.get("type", "rest")
        if ttype not in ("rest", "local"):
            raise ValueError("Invalid 'type', must be 'rest' or 'local'")
        spec: Dict[str, Any] = {
            "name": name,
            "description": description,
            "type": ttype,
            "raw": raw,
        }
        if ttype == "rest":
            endpoint = raw.get("endpoint")
            method = raw.get("method", "GET").upper()
            params = raw.get("params", {})
            if not endpoint or not isinstance(endpoint, str):
                raise ValueError("REST tool requires 'endpoint' string")
            if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                raise ValueError(f"Unsupported HTTP method: {method}")
            spec.update({"endpoint": endpoint, "method": method, "params": params})
        else:  # local
            function_name = raw.get("function")
            if not function_name or not isinstance(function_name, str):
                raise ValueError("Local tool requires 'function' string name")
            spec.update({"function": function_name, "params": raw.get("params", {})})
        return spec

# -------------------------
# AgentManager
# -------------------------
class AgentManager:
    """
    Manages registration of tools with the McpToolRegistrationService.
    Keeps track of registered tools and updates registrations on config changes.
    """

    def __init__(self, registration_service=None):
        # If no service passed, instantiate McpService (real or mock)
        self.service = registration_service() if registration_service else McpService()
        # registered tools mapping: name -> spec
        self.registered: Dict[str, Dict[str, Any]] = {}
        # local function map for 'local' tools
        self.local_functions: Dict[str, Callable[..., Any]] = {}
        # Register built-in local functions
        self._register_builtin_locals()

    def _register_builtin_locals(self):
        # Example local tool: code_formatter
        def code_formatter(code: str, style: Optional[str] = None) -> Dict[str, Any]:
            """
            Very small demo formatter: strips trailing whitespace and normalizes indentation.
            In a real tool, call black/yapf or other formatter.
            """
            if not isinstance(code, str):
                raise ValueError("code must be a string")
            lines = [ln.rstrip() for ln in code.splitlines()]
            # A naive 'format': ensure 4-space indentation for leading tabs
            normalized = []
            for ln in lines:
                normalized.append(ln.replace("\t", " " * 4))
            formatted = "\n".join(normalized).strip() + "\n"
            return {"formatted_code": formatted}

        self.local_functions["code_formatter"] = code_formatter
        logger.debug("Registered builtin local functions: %s", list(self.local_functions.keys()))

    def update_tools(self, tools: Dict[str, Dict[str, Any]]):
        """
        Reconcile the provided `tools` with currently registered tools.
        Register new ones, update changed ones, and unregister removed ones.
        """
        # Figure out names
        desired = set(tools.keys())
        current = set(self.registered.keys())

        to_add = desired - current
        to_remove = current - desired
        to_check = desired & current  # may need update if spec changed

        # Remove obsolete tools
        for name in to_remove:
            try:
                self._unregister_tool(name)
            except Exception:
                logger.exception("Error unregistering tool %s", name)

        # Add new tools
        for name in to_add:
            spec = tools[name]
            try:
                self._register_tool_from_spec(spec)
            except Exception:
                logger.exception("Error registering tool %s", name)

        # Update changed tools
        for name in to_check:
            if tools[name] != self.registered.get(name):
                logger.info("Tool '%s' changed, re-registering", name)
                try:
                    self._unregister_tool(name)
                except Exception:
                    logger.exception("Error unregistering (for update) tool %s", name)
                try:
                    self._register_tool_from_spec(tools[name])
                except Exception:
                    logger.exception("Error re-registering tool %s", name)

    def _register_tool_from_spec(self, spec: Dict[str, Any]):
        name = spec["name"]
        ttype = spec["type"]
        metadata = {"description": spec.get("description", ""), "type": ttype}
        # Create an invocation handler
        if ttype == "rest":
            handler = self._make_rest_handler(spec)
        else:
            handler = self._make_local_handler(spec)
        # Use service to register
        try:
            # Attempt to use common register API name if real SDK: 'register' or 'register_tool'
            if hasattr(self.service, "register_tool"):
                self.service.register_tool(name, metadata, handler)
            elif hasattr(self.service, "register"):
                self.service.register(name, metadata, handler)
            else:
                raise AttributeError("Unsupported registration service API")
            # Track registered spec
            self.registered[name] = copy.deepcopy(spec)
            logger.info("Registered tool '%s' (%s)", name, ttype)
        except Exception as ex:
            logger.exception("Failed to register tool '%s': %s", name, ex)
            raise

    def _unregister_tool(self, name: str):
        # Call service to unregister
        try:
            if hasattr(self.service, "unregister_tool"):
                self.service.unregister_tool(name)
            elif hasattr(self.service, "unregister"):
                self.service.unregister(name)
            else:
                # As fallback, if service is mock, try deleting directly
                if isinstance(self.service, MockMcpToolRegistrationService):
                    self.service.unregister_tool(name)
                else:
                    logger.warning("Registration service does not support unregister operation")
            if name in self.registered:
                del self.registered[name]
            logger.info("Unregistered tool '%s'", name)
        except Exception as ex:
            logger.exception("Error unregistering tool '%s': %s", name, ex)
            raise

    def _make_rest_handler(self, spec: Dict[str, Any]) -> Callable[..., Any]:
        endpoint = spec["endpoint"]
        method = spec["method"].upper()
        default_params = spec.get("params", {})

        def handler(**kwargs):
            # Merge defaults and provided kwargs
            params = {}
            if isinstance(default_params, dict):
                params.update(default_params)
            if kwargs:
                params.update(kwargs)
            try:
                logger.debug("Invoking REST tool '%s' %s %s params=%s", spec["name"], method, endpoint, params)
                if method == "GET":
                    resp = requests.get(endpoint, params=params, timeout=10)
                else:
                    resp = requests.request(method, endpoint, json=params, timeout=10)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError:
                    data = resp.text
                return {"status": "ok", "data": data, "http_status": resp.status_code}
            except Exception as ex:
                logger.exception("REST tool '%s' call failed: %s", spec["name"], ex)
                return {"status": "error", "error": str(ex)}
        return handler

    def _make_local_handler(self, spec: Dict[str, Any]) -> Callable[..., Any]:
        func_name = spec.get("function")
        if func_name not in self.local_functions:
            raise ValueError(f"Local function '{func_name}' not found for tool '{spec['name']}'")
        func = self.local_functions[func_name]

        def handler(**kwargs):
            try:
                logger.debug("Invoking local tool '%s' function='%s' kwargs=%s", spec["name"], func_name, kwargs)
                result = func(**kwargs)
                return {"status": "ok", "result": result}
            except Exception as ex:
                logger.exception("Local tool '%s' failed: %s", spec["name"], ex)
                return {"status": "error", "error": str(ex)}
        return handler

    # Helper to inspect registered tools
    def list_registered(self) -> List[str]:
        try:
            if hasattr(self.service, "list_tools"):
                # Mock service exposes list_tools
                return self.service.list_tools()
        except Exception:
            pass
        return list(self.registered.keys())

# -------------------------
# ConfigWatcher (watchdog-based)
# -------------------------
class ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, registry: ToolRegistry, manager: AgentManager, paths: List[str]):
        super().__init__()
        self.registry = registry
        self.manager = manager
        # normalize absolute paths of interest
        self.paths = {os.path.abspath(p) for p in paths}

    def on_modified(self, event):
        # Filter for the files we care about
        if event.is_directory:
            return
        abspath = os.path.abspath(event.src_path)
        if abspath not in self.paths:
            return
        logger.info("Detected modification of config: %s", event.src_path)
        # Debounce slight bursts by sleeping briefly
        time.sleep(0.1)
        try:
            loaded = self.registry.load_all()
            self.manager.update_tools(loaded)
            logger.info("Reloaded tools after change; registered: %s", self.manager.list_registered())
        except Exception:
            logger.exception("Error reloading tools after change")

class ConfigWatcher:
    """
    Watches config files and triggers reloads via ToolRegistry and AgentManager.
    """
    def __init__(self, registry: ToolRegistry, manager: AgentManager, paths: List[str]):
        self.registry = registry
        self.manager = manager
        self.paths = paths
        self.observer: Optional[Observer] = None

    def start(self):
        event_handler = ConfigChangeHandler(self.registry, self.manager, self.paths)
        obs = Observer()
        # Watch parent dirs for each path (avoid duplicate watchers)
        dirs = {os.path.abspath(os.path.dirname(p)) for p in self.paths}
        for d in dirs:
            if not os.path.isdir(d):
                continue
            logger.info("Starting watcher on directory: %s", d)
            obs.schedule(event_handler, d, recursive=False)
        obs.start()
        self.observer = obs
        logger.info("ConfigWatcher started.")

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            logger.info("ConfigWatcher stopped.")

# -------------------------
# Utilities: create example config files if missing
# -------------------------
SAMPLE_JSON = {
    "tools": [
        {
            "name": "github_repo_search",
            "description": "Search GitHub repositories by query",
            "type": "rest",
            "endpoint": "https://api.github.com/search/repositories",
            "method": "GET",
            "params": {"q": "language:python", "per_page": 3}
        },
        {
            "name": "stack_overflow_search",
            "description": "Fetch StackOverflow questions matching a query",
            "type": "rest",
            "endpoint": "https://api.stackexchange.com/2.3/search/advanced",
            "method": "GET",
            "params": {"order": "desc", "sort": "relevance", "site": "stackoverflow", "pagesize": 3}
        },
        {
            "name": "code_formatter",
            "description": "Format Python code locally",
            "type": "local",
            "function": "code_formatter",
            "params": {"style": "simple"}
        }
    ]
}

SAMPLE_YAML = """
# Alternate config showing same tools in YAML
tools:
  - name: github_repo_search_yaml
    description: "GitHub repo search (YAML-defined)"
    type: rest
    endpoint: "https://api.github.com/search/repositories"
    method: GET
    params:
      q: "language:python"
      per_page: 2

  - name: stack_overflow_search_yaml
    description: "StackOverflow search (YAML-defined)"
    type: rest
    endpoint: "https://api.stackexchange.com/2.3/search/advanced"
    method: GET
    params:
      order: "desc"
      sort: "relevance"
      site: "stackoverflow"
      pagesize: 2
"""

def ensure_example_configs(json_path: str, yaml_path: str):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    if not os.path.exists(json_path):
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE_JSON, f, indent=2)
        logger.info("Wrote example JSON config to %s", json_path)
    else:
        logger.info("JSON config already exists at %s", json_path)
    if not os.path.exists(yaml_path):
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(SAMPLE_YAML)
        logger.info("Wrote example YAML config to %s", yaml_path)
    else:
        logger.info("YAML config already exists at %s", yaml_path)

# -------------------------
# Demo: start agent, watch configs, and demonstrate dynamic addition
# -------------------------
def demo_dynamic_addition(json_path: str, delay: int = 8):
    """
    Wait `delay` seconds, then programmatically add a DockerHub tool to the JSON config
    to demonstrate that the watcher picks it up and registers the tool automatically.
    """
    time.sleep(delay)
    try:
        logger.info("Demo: Adding DockerHub tool to %s", json_path)
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        tools = data.get("tools", [])
        # Avoid duplicate addition
        names = {t.get("name") for t in tools}
        if "dockerhub_search" in names:
            logger.info("DockerHub tool already present; skipping demo addition.")
            return
        docker_tool = {
            "name": "dockerhub_search",
            "description": "Search DockerHub images",
            "type": "rest",
            "endpoint": "https://hub.docker.com/v2/search/repositories",
            "method": "GET",
            "params": {"page_size": 3}
        }
        tools.append(docker_tool)
        data["tools"] = tools
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("DockerHub tool added to %s (watcher should pick this up)", json_path)
    except Exception:
        logger.exception("Failed to add dockerhub tool to demo JSON")

def main():
    # Paths
    base_dir = os.path.abspath(os.path.dirname(__file__))
    config_dir = os.path.join(base_dir, "configs")
    json_path = os.path.join(config_dir, "tools.json")
    yaml_path = os.path.join(config_dir, "tools.yaml")

    # Ensure examples exist
    ensure_example_configs(json_path, yaml_path)

    # Initialize registry and manager
    registry = ToolRegistry([json_path, yaml_path])
    manager = AgentManager()

    # Initial load and register
    loaded = registry.load_all()
    logger.info("Initial load: tools discovered: %s", list(loaded.keys()))
    manager.update_tools(loaded)
    logger.info("Initial registered tools: %s", manager.list_registered())

    # Start watcher
    watcher = ConfigWatcher(registry, manager, [json_path, yaml_path])
    watcher.start()

    # Start demo action in background: add DockerHub tool after delay
    demo_thread = threading.Thread(target=demo_dynamic_addition, args=(json_path, 8), daemon=True)
    demo_thread.start()

    # Interactive demonstration: show how to invoke tools via manager (mock)
    try:
        logger.info("Agent is running. Press Ctrl+C to exit.")
        # Periodically print current registered tools
        while True:
            time.sleep(6)
            registered = manager.list_registered()
            logger.info("Currently registered tools: %s", registered)
            # If mock service, demonstrate invocation of a registered tool if available
            if isinstance(manager.service, MockMcpToolRegistrationService):
                if "code_formatter" in registered:
                    result = manager.service.invoke("code_formatter", code="def  foo():\n\tprint('hi')\n")
                    logger.info("Invoked code_formatter result: %s", result)
                if "github_repo_search" in registered:
                    # call with a small ad-hoc query override
                    res = manager.service.invoke("github_repo_search", q="language:python requests", per_page=1)
                    logger.info("Invoked github_repo_search result keys: %s", list(res.keys()))
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)...")
    finally:
        watcher.stop()

if __name__ == "__main__":
    main()
