#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
安装项目依赖的脚本
"""

import subprocess
import sys

def install_packages():
    packages = [
        "akshare>=1.12.0,<2.0.0",
        "numpy>=1.24.0,<3.0.0", 
        "pandas>=2.0.0,<4.0.0",
        "requests>=2.28.0,<3.0.0"
    ]
    
    for package in packages:
        print(f"正在安装 {package}...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", package], 
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.returncode != 0:
            print(f"安装 {package} 失败:")
            print(result.stderr)
        else:
            print(f"成功安装 {package}")

if __name__ == "__main__":
    install_packages()