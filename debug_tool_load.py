import sys, json
sys.path.append(r'd:/Developer-Assistant-agent/demo Agent')
import agent_core
reg = agent_core.ToolRegistry([r'd:/Developer-Assistant-agent/demo Agent/configs/openapi.yaml'])
loaded = reg.load_all()
print(json.dumps(list(loaded.keys()), indent=2))
