import ast
import datetime
import json
import operator


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, name=None, description="", parameters=None):
        def decorator(func):
            self.register_function(func, name=name, description=description, parameters=parameters)
            return func
        return decorator

    def register_function(self, func, name=None, description="", parameters=None):
        tool_name = name or func.__name__
        self._tools[tool_name] = {
            "func": func,
            "description": description,
            "parameters": parameters or {"type": "object", "properties": {}},
        }
        return func

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return list(self._tools)

    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
            for name, spec in self._tools.items()
        ]

    def execute(self, name, arguments=None):
        spec = self._tools.get(name)
        if spec is None:
            return f"未知工具: {name}"
        try:
            args = json.loads(arguments) if arguments else {}
            result = spec["func"](**args)
        except Exception as e:
            return f"工具 {name} 执行失败: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)


def get_current_time():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_CALC_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def _calc_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_eval(node.left), _calc_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_eval(node.operand))
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


def calculate(expression):
    tree = ast.parse(expression, mode="eval")
    return _calc_eval(tree.body)


default_registry = ToolRegistry()

default_registry.register_function(
    get_current_time,
    description="获取当前日期时间，格式为 YYYY-MM-DD HH:MM:SS",
)

default_registry.register_function(
    calculate,
    description="计算一个算术表达式，支持加减乘除、取模和幂运算，例如 (3 + 4) * 2",
    parameters={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "要计算的算术表达式"}
        },
        "required": ["expression"],
    },
)