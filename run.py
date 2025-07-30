# run.py (终极健壮版 v5.0 - 正确的“魔术处理”顺序 + 健壮的解析器)

import uvicorn
import os
import sys
import logging
import time
import json
from dotenv import load_dotenv

# 确保能找到 src 目录
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 导入 Rich UI 组件
try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.columns import Columns
    from rich.text import Text
except ImportError as e:
    print(f"❌ 关键组件缺失，启动失败: {e}")
    print("👉 请确保您已执行: pip install rich")
    sys.exit(1)

# 仅从 src.auth 导入函数
from src.auth import load_credentials_pool

# ==============================================================================
# ✨ HUGGING FACE-STYLE .ENV "魔术处理" 后台 (v2 - 更健壮) ✨
# ==============================================================================
def preprocess_multiline_env_vars(dotenv_path, console):
    """
    预处理 .env 文件，自动将多行的 GEMINI_CREDENTIALS{i} 变量
    合并为单行并强制注入到环境变量中，覆盖任何由 load_dotenv 加载的错误值。
    """
    if not os.path.exists(dotenv_path):
        return

    console.print("[cyan]✨ 启动 .env 魔术处理后台，修复多行凭证...[/cyan]")
    processed_vars = []

    with open(dotenv_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    line_idx = 0
    while line_idx < len(lines):
        line = lines[line_idx].strip()
        
        # 查找以 "GEMINI_CREDENTIALS" 开头的变量定义
        if line.startswith("GEMINI_CREDENTIALS") and '=' in line:
            var_name, value_part = line.split('=', 1)
            var_name = var_name.strip()
            
            # 如果值部分不包含 '{'，或者 '{' 后面还有内容，说明可能是一个多行JSON
            if '{' in value_part:
                full_value_lines = [value_part]
                brace_count = value_part.count('{') - value_part.count('}')
                
                # 如果括号不平衡，则继续读取下一行
                if brace_count > 0:
                    for next_line_idx in range(line_idx + 1, len(lines)):
                        next_line = lines[next_line_idx]
                        full_value_lines.append(next_line)
                        brace_count += next_line.count('{')
                        brace_count -= next_line.count('}')
                        if brace_count <= 0:
                            line_idx = next_line_idx # 更新主循环的索引
                            break
                
                full_json_str = "".join(full_value_lines)
                try:
                    # 验证并压缩JSON
                    parsed_json = json.loads(full_json_str)
                    minified_json = json.dumps(parsed_json)
                    # 强制注入/覆盖到环境变量
                    os.environ[var_name] = minified_json
                    processed_vars.append(var_name)
                except json.JSONDecodeError:
                    console.print(f"  [yellow]⚠️  警告: {var_name} 的多行值无法解析为JSON。已跳过。[/yellow]")
        
        line_idx += 1

    if processed_vars:
        console.print(f"[green]✅ 魔术处理完成，已成功修复并注入: {', '.join(processed_vars)}[/green]")
    else:
        console.print("[cyan]...未发现需要处理的多行凭证。[/cyan]")
    console.print("──────────")


# --- 核心启动逻辑 ---
if __name__ == "__main__":
    console = Console()
    
    os.system('cls' if os.name == 'nt' else 'clear')

    # 1. 【正确顺序第一步】先运行 load_dotenv()
    # 它会加载简单变量，并对多行变量发出警告（可以忽略）
    load_dotenv(override=True)

    # 2. 【正确顺序第二步】运行“魔术处理”后台
    # 它会用正确的值覆盖掉内存中被污染的变量
    dotenv_path = os.path.join(project_root, '.env')
    preprocess_multiline_env_vars(dotenv_path, console)

    # 初始化日志
    logging.basicConfig(level=logging.INFO, format='%(message)s', handlers=[])
    # 降噪
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    try:
        with console.status("[bold yellow]🚀 正在加载和刷新凭证...", spinner="dots"):
            # 3. 加载凭证，现在它会读到正确的值
            credential_mode, loaded_project_ids = load_credentials_pool(allow_oauth_flow=False)

        console.print("✅ [bold green]凭证加载完成！[/bold green]")
        console.print(f"   检测到模式: [yellow]{credential_mode}[/yellow], 共 {len(loaded_project_ids)} 个凭证。")
        console.print("\n[cyan]15秒后将清屏并显示服务摘要... (此期间服务未启动)[/cyan]")
        
        time.sleep(15)

        HOST = os.getenv("HOST", "0.0.0.0")
        PORT = int(os.getenv("PORT", "8888"))
        
        # 准备UI内容...（代码与之前版本相同，此处为完整版）
        listening_address = f"http://{HOST}:{PORT}"
        renderable_items = [
            Text.from_markup(f"🌐 [bold]监听地址[/bold]: [cyan bold]{listening_address}[/cyan bold]"),
            Text.from_markup(f"💻 [bold]本地调用[/bold]: [cyan bold]http://127.0.0.1:{PORT}[/cyan bold]"),
            Text("──────────"),
            Text.from_markup(f"⚙️  [bold]凭证模式[/bold]: [yellow]{credential_mode}[/yellow]"),
        ]
        if loaded_project_ids:
            renderable_items.append(Text.from_markup(f"🔑 [bold]可用凭证 [/bold]({len(loaded_project_ids)} 个):"))
            credential_items = [Text.from_markup(f"  [green]✓ {pid}[/green]") for pid in loaded_project_ids]
            renderable_items.append(Columns(credential_items, equal=True, expand=True))
        else:
            renderable_items.append(Text.from_markup(f"⚠️  [bold red]警告: 未加载任何有效凭证，API 调用将失败。[/bold red]"))
        renderable_items.append(Text("──────────"))
        renderable_items.append(Text.from_markup(f"💡 [yellow]提示: 按 [CTRL + C] 可随时停止服务。[/yellow]"))
        summary_renderable = Group(*renderable_items)

        # 再次清屏，绘制最终面板
        os.system('cls' if os.name == 'nt' else 'clear')
        console.print(Panel(
            summary_renderable,
            title=Text.from_markup("🚀 [bold green]Gemini 代理服务已就绪[/bold green]"),
            border_style="blue",
            expand=False,
            padding=(1, 2)
        ))
        console.print("")

    except Exception as e:
        console.print(Panel(
            f"[bold red]❌ 服务启动失败！[/bold red]\n\n请检查以下错误信息：\n\n[white]{e}[/white]",
            title="[bold red]错误[/bold red]",
            border_style="red"
        ))
        console.print_exception(show_locals=True)
        sys.exit(1)

    # 4. 【最后一步】启动FastAPI/Uvicorn服务器
    try:
        # 关掉uvicorn自带的日志，让界面更干净
        uvicorn_log_config = uvicorn.config.LOGGING_CONFIG
        uvicorn_log_config["formatters"]["default"]["fmt"] = "%(levelprefix)s %(message)s"
        uvicorn_log_config["formatters"]["access"]["fmt"] = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
        uvicorn.run(
            "src.main:app", 
            host=HOST, 
            port=PORT, 
            log_config=uvicorn_log_config
        )
    except Exception as e:
        console.print(f"[bold red]❌ Uvicorn 服务器运行出错: {e}[/bold red]")
        sys.exit(1)
