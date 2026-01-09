# Developer Assistant Agent Demo

This demo shows a Python-based Developer Assistant Agent using the Microsoft Agent Framework (or a mock) that dynamically registers tools from JSON/YAML configs and updates them on file changes.

Setup

```bash
pip install -r requirements.txt
python demo_agent.py
```

Files

- `demo_agent.py`: single-file demo containing `ToolRegistry`, `AgentManager`, and `ConfigWatcher`.
- `configs/tools.json` and `configs/tools.yaml`: example tool configs created if missing.

