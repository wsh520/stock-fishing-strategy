import ast

try:
    with open('src/bottom_fishing_strategy.py', 'r', encoding='utf-8') as f:
        content = f.read()
    ast.parse(content)
    print('语法检查通过')
except SyntaxError as e:
    print(f'语法错误: {e}')
except Exception as e:
    print(f'其他错误: {e}')