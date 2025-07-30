# run.py (终极解决方案)

import uvicorn
import logging
import os
import sys

# --- 1. 路径修复 ---
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# --- 2. 日志配置 (在 Uvicorn 启动前完成) ---
# 仅使用我们自己的格式，完全接管日志输出
logging.basicConfig(level=logging.INFO, format='%(message)s')

# 屏蔽 httpx 等第三方库的冗余 INFO 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
# 【可选但推荐】为了以防万一，再次清理uvicorn的根日志处理器
logging.getLogger("uvicorn").handlers = []
logging.getLogger("uvicorn.error").propagate = True


if __name__ == "__main__":
    logging.info("🚀 服务启动中... 将使用终极精简日志。")
    
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8888,
        log_config=None,  # 告诉 Uvicorn 不要使用它自己的日志配置字典
        access_log=False  # <--- 最核心的修改：从源头彻底禁用默认访问日志
    )