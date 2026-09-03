#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
测试依赖库是否正确安装
"""

def test_imports():
    try:
        import akshare
        print(f"akshare 版本: {akshare.__version__}")
    except ImportError as e:
        print(f"akshare 导入失败: {e}")
    
    try:
        import pandas
        print(f"pandas 版本: {pandas.__version__}")
    except ImportError as e:
        print(f"pandas 导入失败: {e}")
    
    try:
        import numpy
        print(f"numpy 版本: {numpy.__version__}")
    except ImportError as e:
        print(f"numpy 导入失败: {e}")
    
    try:
        import requests
        print(f"requests 版本: {requests.__version__}")
    except ImportError as e:
        print(f"requests 导入失败: {e}")
    
    print("所有依赖库测试完成")

if __name__ == "__main__":
    test_imports()