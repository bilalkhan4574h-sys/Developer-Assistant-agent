"""
Core agent module extracted from demo_agent.py for reuse by the web UI.
Provides: ToolRegistry, AgentManager, ConfigWatcher and helper functions.
"""
from __future__ import annotations
import os
import json
import time
import yaml
import threading
import logging
import copy
import requests
from typing import Dict, Any, Callable, Optional, List

# Watchdog imports
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agent-core")

# Mock registration service (same as demo)
class MockMcpToolRegistrationService:
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
        return handler(**kwargs)


McpService = MockMcpToolRegistrationService


class ToolRegistry:
    def __init__(self, config_paths: List[str]):
        self.config_paths = config_paths
        self.tools: Dict[str, Dict[str, Any]] = {}

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        loaded: Dict[str, Dict[str, Any]] = {}
        for path in self.config_paths:
            if not os.path.exists(path):
                logger.warning("Config path not found: %s", path)
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                if path.endswith('.json'):
                    parsed = json.loads(text)
                else:
                    parsed = yaml.safe_load(text)
                # If OpenAPI/Swagger document, convert to tool entries
                if isinstance(parsed, dict) and ("openapi" in parsed or "swagger" in parsed):
                    logger.info("Detected OpenAPI/Swagger document at %s; converting to tools", path)
                    entries = self._convert_openapi(parsed)
                    logger.info("Converted %d operations from OpenAPI at %s", len(entries), path)
                else:
                    entries = parsed.get('tools') if isinstance(parsed, dict) and 'tools' in parsed else parsed
                if not isinstance(entries, list):
                    logger.error("Config at %s must contain a list of tool entries", path)
                    continue
                for entry in entries:
                    try:
                        norm = self._validate_and_normalize(entry)
                        loaded[norm['name']] = norm
                    except ValueError as ex:
                        logger.error("Invalid tool entry in %s: %s", path, ex)
            except Exception as ex:
                logger.exception("Failed to read/parse config %s: %s", path, ex)
        self.tools = loaded
        return loaded

    def _convert_openapi(self, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Convert an OpenAPI (v3) or Swagger (v2) spec dict into a list of tool entries
        compatible with the ToolRegistry format. Each operation is turned into a
        REST tool whose `endpoint` is the server URL + path and method is the HTTP verb.
        OperationId is used as the tool `name` when present; otherwise a name is
        generated from method and path.
        """
        entries: List[Dict[str, Any]] = []
        servers = []
        if "servers" in spec and isinstance(spec["servers"], list):
            for s in spec["servers"]:
                url = s.get("url") if isinstance(s, dict) else None
                if url:
                    servers.append(url.rstrip("/"))
        if not servers and spec.get("swagger") == "2.0":
            host = spec.get("host", "")
            basePath = spec.get("basePath", "")
            if host:
                servers.append((host + basePath).rstrip("/"))
        if not servers:
            servers = [""]
        paths = spec.get("paths", {}) or {}
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in ("get", "post", "put", "delete", "patch"):
                    continue
                op = op or {}
                name = op.get("operationId") or f"{method.lower()}_{path.strip('/').replace('/', '_').replace('{','').replace('}','') or 'root'}"
                summary = op.get("summary") or op.get("description") or ""
                params: Dict[str, Any] = {}
                for p in op.get("parameters", []) or []:
                    pname = p.get("name")
                    if not pname:
                        continue
                    params[pname] = p.get("schema") or p.get("example") or p.get("default") or None
                if op.get("requestBody"):
                    params["body"] = None
                base = servers[0]
                endpoint = base + path
                entry = {
                    "name": name,
                    "description": summary,
                    "type": "rest",
                    "endpoint": endpoint,
                    "method": method.upper(),
                    "params": params,
                }
                entries.append(entry)
        return entries

    def _validate_and_normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError('Tool entry must be an object')
        name = raw.get('name')
        if not name or not isinstance(name, str):
            raise ValueError("Missing or invalid 'name'")
        description = raw.get('description', '')
        ttype = raw.get('type', 'rest')
        if ttype not in ('rest', 'local'):
            raise ValueError("Invalid 'type', must be 'rest' or 'local'")
        spec: Dict[str, Any] = {'name': name, 'description': description, 'type': ttype, 'raw': raw}
        if ttype == 'rest':
            endpoint = raw.get('endpoint')
            method = raw.get('method', 'GET').upper()
            params = raw.get('params', {})
            if not endpoint or not isinstance(endpoint, str):
                raise ValueError("REST tool requires 'endpoint' string")
            if method not in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
                raise ValueError(f"Unsupported HTTP method: {method}")
            spec.update({'endpoint': endpoint, 'method': method, 'params': params})
        else:
            function_name = raw.get('function')
            if not function_name or not isinstance(function_name, str):
                raise ValueError("Local tool requires 'function' string name")
            spec.update({'function': function_name, 'params': raw.get('params', {})})
        return spec


class AgentManager:
    def __init__(self, registration_service=None):
        self.service = registration_service() if registration_service else McpService()
        self.registered: Dict[str, Dict[str, Any]] = {}
        self.local_functions: Dict[str, Callable[..., Any]] = {}
        self._register_builtin_locals()

    def _register_builtin_locals(self):
        def code_formatter(code: str, style: Optional[str] = None) -> Dict[str, Any]:
            if not isinstance(code, str):
                raise ValueError('code must be a string')
            lines = [ln.rstrip() for ln in code.splitlines()]
            normalized = [ln.replace('\t', ' ' * 4) for ln in lines]
            formatted = '\n'.join(normalized).strip() + '\n'
            return {'formatted_code': formatted}

        self.local_functions['code_formatter'] = code_formatter
        
        def research_search(query: str, top_k: int = 5, docs_dir: Optional[str] = None) -> Dict[str, Any]:
            """
            Simple local research search: scans text and markdown files under `papers/` (or provided dir),
            ranks by term overlap and returns top_k results with snippets.
            """
            if not isinstance(query, str) or not query.strip():
                return {"results": []}
            base = docs_dir or os.path.join(os.path.dirname(__file__), 'papers')
            results = []
            terms = [t.lower() for t in query.split() if t.strip()]
            try:
                for root, _, files in os.walk(base):
                    for fn in files:
                        if not fn.lower().endswith(('.txt', '.md')):
                            continue
                        path = os.path.join(root, fn)
                        try:
                            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                                txt = f.read()
                        except Exception:
                            continue
                        lower = txt.lower()
                        score = 0
                        for t in terms:
                            score += lower.count(t)
                        if score <= 0:
                            continue
                        # find first occurrence snippet
                        idx = min((lower.find(t) for t in terms if t in lower), default=-1)
                        if idx >= 0:
                            start = max(0, idx - 120)
                            end = min(len(txt), idx + 240)
                            snippet = txt[start:end].replace('\n', ' ')
                        else:
                            snippet = txt[:240].replace('\n', ' ')
                        results.append({"file": os.path.relpath(path, base), "score": score, "snippet": snippet})
                results.sort(key=lambda x: x['score'], reverse=True)
                return {"results": results[:top_k]}
            except Exception as ex:
                logger.exception('research_search failed: %s', ex)
                return {"error": str(ex), "results": []}

        self.local_functions['research_search'] = research_search

    def update_tools(self, tools: Dict[str, Dict[str, Any]]):
        desired = set(tools.keys())
        current = set(self.registered.keys())
        to_add = desired - current
        to_remove = current - desired
        to_check = desired & current
        for name in to_remove:
            try:
                self._unregister_tool(name)
            except Exception:
                logger.exception('Error unregistering tool %s', name)
        for name in to_add:
            spec = tools[name]
            try:
                self._register_tool_from_spec(spec)
            except Exception:
                logger.exception('Error registering tool %s', name)
        for name in to_check:
            if tools[name] != self.registered.get(name):
                logger.info("Tool '%s' changed, re-registering", name)
                try:
                    self._unregister_tool(name)
                except Exception:
                    logger.exception('Error unregistering (for update) tool %s', name)
                try:
                    self._register_tool_from_spec(tools[name])
                except Exception:
                    logger.exception('Error re-registering tool %s', name)

    def _register_tool_from_spec(self, spec: Dict[str, Any]):
        name = spec['name']
        ttype = spec['type']
        metadata = {'description': spec.get('description', ''), 'type': ttype}
        if ttype == 'rest':
            handler = self._make_rest_handler(spec)
        else:
            handler = self._make_local_handler(spec)
        if hasattr(self.service, 'register_tool'):
            self.service.register_tool(name, metadata, handler)
        elif hasattr(self.service, 'register'):
            self.service.register(name, metadata, handler)
        else:
            raise AttributeError('Unsupported registration service API')
        self.registered[name] = copy.deepcopy(spec)
        logger.info("Registered tool '%s' (%s)", name, ttype)

    def _unregister_tool(self, name: str):
        if hasattr(self.service, 'unregister_tool'):
            self.service.unregister_tool(name)
        elif hasattr(self.service, 'unregister'):
            self.service.unregister(name)
        else:
            if isinstance(self.service, MockMcpToolRegistrationService):
                self.service.unregister_tool(name)
            else:
                logger.warning('Registration service does not support unregister operation')
        if name in self.registered:
            del self.registered[name]
        logger.info("Unregistered tool '%s'", name)

    def _make_rest_handler(self, spec: Dict[str, Any]) -> Callable[..., Any]:
        endpoint = spec['endpoint']
        method = spec['method'].upper()
        default_params = spec.get('params', {})

        def handler(**kwargs):
            params = {}
            if isinstance(default_params, dict):
                params.update(default_params)
            if kwargs:
                params.update(kwargs)
            try:
                if method == 'GET':
                    resp = requests.get(endpoint, params=params, timeout=10)
                else:
                    resp = requests.request(method, endpoint, json=params, timeout=10)
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError:
                    data = resp.text
                return {'status': 'ok', 'data': data, 'http_status': resp.status_code}
            except Exception as ex:
                logger.exception("REST tool '%s' call failed: %s", spec['name'], ex)
                return {'status': 'error', 'error': str(ex)}

        return handler

    def _make_local_handler(self, spec: Dict[str, Any]) -> Callable[..., Any]:
        func_name = spec.get('function')
        if func_name not in self.local_functions:
            raise ValueError(f"Local function '{func_name}' not found for tool '{spec['name']}'")
        func = self.local_functions[func_name]

        def handler(**kwargs):
            try:
                result = func(**kwargs)
                return {'status': 'ok', 'result': result}
            except Exception as ex:
                logger.exception("Local tool '%s' failed: %s", spec['name'], ex)
                return {'status': 'error', 'error': str(ex)}

        return handler

    def list_registered(self) -> List[str]:
        if hasattr(self.service, 'list_tools'):
            return self.service.list_tools()
        return list(self.registered.keys())


class ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, registry: ToolRegistry, manager: AgentManager, paths: List[str]):
        super().__init__()
        self.registry = registry
        self.manager = manager
        self.paths = {os.path.abspath(p) for p in paths}

    def on_modified(self, event):
        if event.is_directory:
            return
        abspath = os.path.abspath(event.src_path)
        if abspath not in self.paths:
            return
        logger.info("Detected modification of config: %s", event.src_path)
        time.sleep(0.1)
        try:
            loaded = self.registry.load_all()
            self.manager.update_tools(loaded)
            logger.info("Reloaded tools after change; registered: %s", self.manager.list_registered())
        except Exception:
            logger.exception("Error reloading tools after change")


class ConfigWatcher:
    def __init__(self, registry: ToolRegistry, manager: AgentManager, paths: List[str]):
        self.registry = registry
        self.manager = manager
        self.paths = paths
        self.observer: Optional[Observer] = None

    def start(self):
        event_handler = ConfigChangeHandler(self.registry, self.manager, self.paths)
        obs = Observer()
        dirs = {os.path.abspath(os.path.dirname(p)) for p in self.paths}
        for d in dirs:
            if not os.path.isdir(d):
                continue
            obs.schedule(event_handler, d, recursive=False)
        obs.start()
        self.observer = obs

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
