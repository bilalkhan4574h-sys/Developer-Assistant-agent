"""
Flask-based web UI to interact with the Agent core.
Provides endpoints to view tools, invoke tools, and edit configs.
"""
from __future__ import annotations
import os
import json
import threading
import logging
from flask import Flask, jsonify, request, render_template, send_from_directory
from agent_core import ToolRegistry, AgentManager, ConfigWatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("webui")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "configs")
JSON_PATH = os.path.join(CONFIG_DIR, "tools.json")
YAML_PATH = os.path.join(CONFIG_DIR, "tools.yaml")

app = Flask(__name__, template_folder="templates", static_folder="static")

# Initialize agent components
registry = ToolRegistry([JSON_PATH, YAML_PATH])
manager = AgentManager()
loaded = registry.load_all()
manager.update_tools(loaded)
watcher = ConfigWatcher(registry, manager, [JSON_PATH, YAML_PATH])
watcher.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/tools', methods=['GET'])
def api_tools():
    tools = registry.load_all()
    return jsonify({'tools': list(tools.values())})

@app.route('/api/registered', methods=['GET'])
def api_registered():
    return jsonify({'registered': manager.list_registered()})

@app.route('/api/invoke', methods=['POST'])
def api_invoke():
    body = request.json or {}
    name = body.get('name')
    params = body.get('params', {})
    if not name:
        return jsonify({'status': 'error', 'error': 'Missing tool name'}), 400
    try:
        # use manager.service.invoke for mock, else try to call via registered handler
        if hasattr(manager.service, 'invoke'):
            result = manager.service.invoke(name, **params)
        else:
            # fallback: call local registered handler
            handler_spec = manager.registered.get(name)
            if not handler_spec:
                return jsonify({'status': 'error', 'error': 'Tool not registered'}), 404
            # Recreate handler and call
            if handler_spec['type'] == 'local':
                handler = manager._make_local_handler(handler_spec)
            else:
                handler = manager._make_rest_handler(handler_spec)
            result = handler(**params)
        return jsonify({'status': 'ok', 'result': result})
    except Exception as ex:
        logger.exception('Invocation failed')
        return jsonify({'status': 'error', 'error': str(ex)}), 500

@app.route('/api/config', methods=['GET'])
def api_get_config():
    path = request.args.get('path', 'json')
    target = JSON_PATH if path == 'json' else YAML_PATH
    try:
        with open(target, 'r', encoding='utf-8') as f:
            text = f.read()
        return jsonify({'path': target, 'content': text})
    except Exception as ex:
        logger.exception('Read config failed')
        return jsonify({'status': 'error', 'error': str(ex)}), 500

@app.route('/api/config', methods=['POST'])
def api_save_config():
    body = request.json or {}
    path = body.get('path', 'json')
    content = body.get('content')
    if content is None:
        return jsonify({'status': 'error', 'error': 'Missing content'}), 400
    target = JSON_PATH if path == 'json' else YAML_PATH
    try:
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        # Trigger immediate reload
        tools = registry.load_all()
        manager.update_tools(tools)
        return jsonify({'status': 'ok'})
    except Exception as ex:
        logger.exception('Save config failed')
        return jsonify({'status': 'error', 'error': str(ex)}), 500

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

def run_app(host='127.0.0.1', port=5000):
    app.run(host=host, port=port, debug=False)

if __name__ == '__main__':
    run_app()
