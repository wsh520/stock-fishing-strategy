import re
import chardet

def clean_file_encoding():
    # 读取二进制内容
    with open('src/bottom_fishing_strategy.py', 'rb') as f:
        binary_content = f.read()
    
    # 检测编码
    detected = chardet.detect(binary_content)
    print(f"检测到的编码: {detected}")
    
    # 尝试多种编码方式
    encodings = [detected['encoding'], 'utf-8', 'gbk', 'gb2312', 'latin1', 'cp1252']
    
    content = None
    for enc in encodings:
        if enc is None:
            continue
        try:
            content = binary_content.decode(enc)
            print(f"成功使用 {enc} 解码")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    
    if content is None:
        print("无法解码文件")
        return False
    
    # 清理乱码字符，只保留ASCII和有效的中文字符
    cleaned_lines = []
    for line_num, line in enumerate(content.split('\n'), 1):
        try:
            # 尝试编码为UTF-8，过滤掉无效字符
            clean_line = line.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
            cleaned_lines.append(clean_line)
        except:
            # 如果还是有问题，就逐字符处理
            clean_chars = []
            for char in line:
                # 保留ASCII字符和常见中文字符范围
                if ord(char) < 128 or (0x4e00 <= ord(char) <= 0x9fff):
                    clean_chars.append(char)
                else:
                    # 替换乱码字符为空格
                    clean_chars.append(' ')
            cleaned_lines.append(''.join(clean_chars))
    
    cleaned_content = '\n'.join(cleaned_lines)
    
    # 删除回测相关函数
    # 1. 删除 save_signal_history 函数
    cleaned_content = re.sub(r'def save_signal_history\(.*?\).*?^(\s*def |\s*if __name__|class |\Z)', 
                             r'\1', cleaned_content, flags=re.DOTALL|re.MULTILINE)
    
    # 2. 删除 weekly_performance_review 函数  
    cleaned_content = re.sub(r'def weekly_performance_review\(.*?\).*?^(\s*def |\s*if __name__|class |\Z)', 
                             r'\1', cleaned_content, flags=re.DOTALL|re.MULTILINE)
    
    # 3. 删除 update_signal_status 函数
    cleaned_content = re.sub(r'def update_signal_status\(.*?\).*?^(\s*def |\s*if __name__|class |\Z)', 
                             r'\1', cleaned_content, flags=re.DOTALL|re.MULTILINE)
    
    # 4. 修改 __main__ 部分，移除回测相关的模式
    cleaned_content = re.sub(
        r'    elif mode == "track":.*?cfg = StrategyConfig\(\)\n.*?weekly_performance_review\(cfg\).*?print\(.*?"无追踪数据".*?\)',
        '    elif mode == "track":\n        print("跟踪功能已移除")',
        cleaned_content,
        flags=re.DOTALL
    )
    
    # 5. 修改 full 模式，移除回测相关功能
    cleaned_content = re.sub(
        r'    elif mode == "full":.*?save_signal_history\(result\).*?weekly_performance_review\(cfg\).*?update_signal_status\(report\)',
        '    elif mode == "full":\n        result = main()\n        if result is not None:\n            print(f"共 {len(result)} 只信号")\n        print("回测和跟踪功能已移除")',
        cleaned_content,
        flags=re.DOTALL
    )
    
    # 保存清理后的内容
    with open('src/bottom_fishing_strategy.py', 'w', encoding='utf-8') as f:
        f.write(cleaned_content)
    
    print("文件已清理并保存")
    return True

if __name__ == "__main__":
    clean_file_encoding()