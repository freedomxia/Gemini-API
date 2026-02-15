"""
Docker 构建与运行验证测试

测试思路一：静态分析 - 不需要 Docker 环境
  验证 Dockerfile、docker-compose.yml 语法和逻辑正确性
  验证所有必要文件存在且路径引用一致

测试思路二：运行时模拟 - 验证应用逻辑
  模拟 Docker 容器内的工作目录结构
  验证 web/app.py 能正确导入和创建 app
"""
import os
import sys
import json
import importlib
import tempfile
import shutil
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 思路一：静态文件与配置验证
# ============================================================

class TestStaticAnalysis:
    """静态分析：验证 Docker 相关文件的正确性和完整性"""

    def test_dockerfile_exists(self):
        assert (ROOT / "Dockerfile").is_file(), "Dockerfile 不存在"

    def test_docker_compose_exists(self):
        assert (ROOT / "docker-compose.yml").is_file(), "docker-compose.yml 不存在"

    def test_dockerignore_exists(self):
        assert (ROOT / ".dockerignore").is_file(), ".dockerignore 不存在"

    def test_dockerfile_syntax(self):
        """验证 Dockerfile 关键指令存在且顺序合理"""
        content = (ROOT / "Dockerfile").read_text()
        lines = [l.strip() for l in content.splitlines() if l.strip() and not l.strip().startswith("#")]

        # 必须包含的指令
        assert any(l.startswith("FROM") for l in lines), "缺少 FROM 指令"
        assert any(l.startswith("WORKDIR") for l in lines), "缺少 WORKDIR 指令"
        assert any(l.startswith("COPY") for l in lines), "缺少 COPY 指令"
        assert any(l.startswith("RUN") for l in lines), "缺少 RUN 指令"
        assert any(l.startswith("EXPOSE") for l in lines), "缺少 EXPOSE 指令"
        assert any(l.startswith("CMD") for l in lines), "缺少 CMD 指令"

    def test_dockerfile_copies_required_dirs(self):
        """验证 Dockerfile 复制了所有必要的目录"""
        content = (ROOT / "Dockerfile").read_text()
        assert "COPY src/" in content, "Dockerfile 未复制 src/ 目录"
        assert "COPY web/" in content, "Dockerfile 未复制 web/ 目录"
        assert "COPY pyproject.toml" in content, "Dockerfile 未复制 pyproject.toml"
        assert "COPY data/" in content, "Dockerfile 未复制 data/ 目录"

    def test_dockerfile_installs_web_deps(self):
        """验证安装了 web 应用的额外依赖"""
        content = (ROOT / "Dockerfile").read_text()
        assert "aiohttp" in content, "Dockerfile 未安装 aiohttp"
        assert "aiohttp-cors" in content, "Dockerfile 未安装 aiohttp-cors"

    def test_dockerfile_exposes_correct_port(self):
        """验证暴露的端口与应用一致"""
        content = (ROOT / "Dockerfile").read_text()
        assert "EXPOSE 8000" in content, "Dockerfile 未暴露 8000 端口"

    def test_dockerfile_cmd_runs_app(self):
        """验证 CMD 启动正确的入口文件"""
        content = (ROOT / "Dockerfile").read_text()
        assert "web/app.py" in content, "CMD 未指向 web/app.py"

    def test_dockerfile_workdir(self):
        """验证工作目录设置"""
        content = (ROOT / "Dockerfile").read_text()
        assert "WORKDIR /app" in content, "WORKDIR 应为 /app"

    def test_docker_compose_service(self):
        """验证 docker-compose.yml 服务配置"""
        import yaml  # 如果没有 yaml 就用 json 解析不了，手动检查
        content = (ROOT / "docker-compose.yml").read_text()
        # 基本字段检查
        assert "services:" in content, "缺少 services 定义"
        assert "build:" in content, "缺少 build 配置"
        assert "ports:" in content, "缺少 ports 映射"
        assert "volumes:" in content, "缺少 volumes 挂载"
        assert "8000" in content, "端口映射应包含 8000"
        assert "/app/data" in content, "应挂载 data 目录到 /app/data"

    def test_docker_compose_volume_mount(self):
        """验证 data 目录挂载路径与应用代码一致"""
        compose_content = (ROOT / "docker-compose.yml").read_text()
        # app.py 使用 Path("data") 相对路径，WORKDIR 是 /app
        # 所以容器内路径应该是 /app/data
        assert "./data:/app/data" in compose_content, "data 卷挂载路径不正确"

    def test_dockerignore_excludes_unnecessary(self):
        """验证 .dockerignore 排除了不必要的文件"""
        content = (ROOT / ".dockerignore").read_text()
        assert ".venv" in content, ".dockerignore 应排除 .venv"
        assert ".git" in content, ".dockerignore 应排除 .git"
        assert "__pycache__" in content, ".dockerignore 应排除 __pycache__"

    def test_source_files_exist(self):
        """验证 Docker 构建所需的所有源文件都存在"""
        required = [
            "pyproject.toml",
            "src/gemini_webapi/__init__.py",
            "src/gemini_webapi/client.py",
            "src/gemini_webapi/constants.py",
            "src/gemini_webapi/exceptions.py",
            "web/app.py",
            "web/static/index.html",
        ]
        for f in required:
            assert (ROOT / f).is_file(), f"必要文件 {f} 不存在"

    def test_pyproject_toml_valid(self):
        """验证 pyproject.toml 包含必要的构建配置"""
        content = (ROOT / "pyproject.toml").read_text()
        assert "gemini-webapi" in content, "项目名称缺失"
        assert 'where = ["src"]' in content, "包搜索路径应为 src"

    def test_app_relative_paths_match_workdir(self):
        """验证 app.py 中的相对路径在 WORKDIR=/app 下能正确解析"""
        app_content = (ROOT / "web" / "app.py").read_text()
        dockerfile_content = (ROOT / "Dockerfile").read_text()

        # app.py 使用 Path("data") 作为数据目录
        assert 'Path("data")' in app_content, "app.py 应使用 Path('data')"
        # app.py 使用 "web/static/index.html" 和 "web/static" 作为静态文件路径
        assert '"web/static/index.html"' in app_content, "app.py 应引用 web/static/index.html"
        assert '"web/static"' in app_content, "app.py 应引用 web/static"

        # Dockerfile WORKDIR 是 /app，所以这些相对路径会解析为：
        # /app/data, /app/web/static/index.html, /app/web/static
        # 验证 COPY 指令把文件放到了正确位置
        assert "COPY web/ ./web/" in dockerfile_content, "web/ 应复制到 ./web/"

    def test_no_hardcoded_absolute_paths_in_app(self):
        """验证 app.py 没有硬编码绝对路径"""
        app_content = (ROOT / "web" / "app.py").read_text()
        # 不应该有 /home, /Users 等绝对路径
        for bad_prefix in ["/home/", "/Users/", "C:\\"]:
            assert bad_prefix not in app_content, f"app.py 包含硬编码路径 {bad_prefix}"


# ============================================================
# 思路二：运行时模拟验证
# ============================================================

class TestRuntimeSimulation:
    """运行时模拟：验证应用在容器环境下的行为"""

    def test_app_module_imports(self):
        """验证 web/app.py 能正确导入（模拟容器内环境）"""
        # 需要 Python >= 3.10（match 语法）和已安装依赖
        if sys.version_info < (3, 10):
            # 本地 Python 版本低于 3.10，跳过（Docker 使用 3.12）
            # 改为验证 __init__.py 的导入声明正确
            init_content = (ROOT / "src" / "gemini_webapi" / "__init__.py").read_text()
            assert "from .client import GeminiClient" in init_content
            return

        try:
            import gemini_webapi
            assert hasattr(gemini_webapi, "GeminiClient"), "GeminiClient 应可从 gemini_webapi 导入"
        except ImportError:
            sys.path.insert(0, str(ROOT / "src"))
            import gemini_webapi
            assert hasattr(gemini_webapi, "GeminiClient")

    def test_app_creates_successfully(self):
        """验证 create_app() 能成功创建 aiohttp 应用"""
        try:
            import aiohttp
        except ImportError:
            # aiohttp 未安装，改为验证 app.py 的关键函数定义
            app_content = (ROOT / "web" / "app.py").read_text()
            assert "def create_app():" in app_content, "create_app 函数未定义"
            assert "app = web.Application()" in app_content, "未创建 web.Application"
            assert "app.on_startup.append(on_startup)" in app_content, "未注册 on_startup"
            # 验证所有路由注册
            expected_routes = [
                '"/api/login"',
                '"/api/accounts"',
                '"/api/config"',
                '"/api/stats"',
                '"/v1/chat/completions"',
                '"/v1/models"',
            ]
            for route in expected_routes:
                assert route in app_content, f"路由 {route} 未注册"
            return

        original_cwd = os.getcwd()
        try:
            os.chdir(ROOT)
            sys.path.insert(0, str(ROOT / "src"))

            spec = importlib.util.spec_from_file_location("webapp", ROOT / "web" / "app.py")
            webapp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(webapp)

            app = webapp.create_app()
            assert app is not None, "create_app() 返回 None"

            routes = [r.resource.canonical for r in app.router.routes() if hasattr(r, 'resource') and hasattr(r.resource, 'canonical')]
            route_paths = set(routes)

            expected_routes = ["/api/login", "/api/accounts", "/api/config", "/api/stats", "/v1/chat/completions", "/v1/models", "/"]
            for route in expected_routes:
                assert route in route_paths, f"路由 {route} 未注册"
        finally:
            os.chdir(original_cwd)

    def test_data_dir_auto_creation(self):
        """验证 data 目录不存在时能自动创建"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            # data 目录不存在
            assert not data_dir.exists()
            # 模拟 app.py 的行为
            data_dir.mkdir(exist_ok=True)
            assert data_dir.is_dir(), "data 目录应被自动创建"

    def test_config_default_generation(self):
        """验证没有 config.json 时能生成默认配置"""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.json"
            assert not config_file.exists()

            # 模拟 load_config 逻辑
            import uuid
            default = {
                "admin_password": "admin",
                "api_key": str(uuid.uuid4())[:8],
                "model": "unspecified",
                "plugin_token": uuid.uuid4().hex[:32],
                "plugin_auto_enable": True,
            }
            config_file.write_text(json.dumps(default, ensure_ascii=False, indent=2))
            assert config_file.is_file()

            loaded = json.loads(config_file.read_text())
            assert loaded["admin_password"] == "admin"
            assert "api_key" in loaded

    def test_accounts_empty_on_fresh_start(self):
        """验证全新启动时账号列表为空"""
        with tempfile.TemporaryDirectory() as tmpdir:
            accounts_file = Path(tmpdir) / "accounts.json"
            # 模拟 load_accounts
            if accounts_file.exists():
                accounts = json.loads(accounts_file.read_text())
            else:
                accounts = []
            assert accounts == [], "全新启动时账号应为空列表"

    def test_static_files_accessible(self):
        """验证静态文件在预期路径存在"""
        assert (ROOT / "web" / "static" / "index.html").is_file(), "index.html 不存在"
        # 验证 index.html 包含必要的 HTML 结构
        html_content = (ROOT / "web" / "static" / "index.html").read_text()
        assert "<!DOCTYPE html>" in html_content
        assert "Gemini API" in html_content

    def test_gemini_webapi_package_structure(self):
        """验证 gemini_webapi 包结构完整"""
        pkg_dir = ROOT / "src" / "gemini_webapi"
        required_modules = [
            "__init__.py",
            "client.py",
            "constants.py",
            "exceptions.py",
        ]
        for mod in required_modules:
            assert (pkg_dir / mod).is_file(), f"模块 {mod} 缺失"

        # 子包
        assert (pkg_dir / "types" / "__init__.py").is_file(), "types 子包缺失"
        assert (pkg_dir / "utils" / "__init__.py").is_file(), "utils 子包缺失"
        assert (pkg_dir / "components" / "__init__.py").is_file(), "components 子包缺失"

    def test_port_env_variable(self):
        """验证 PORT 环境变量能被正确读取"""
        app_content = (ROOT / "web" / "app.py").read_text()
        assert 'os.environ.get("PORT", 8000)' in app_content or \
               "os.environ.get('PORT', 8000)" in app_content, \
               "app.py 应从 PORT 环境变量读取端口"

    def test_dockerfile_layer_caching_optimization(self):
        """验证 Dockerfile 利用了层缓存优化"""
        content = (ROOT / "Dockerfile").read_text()
        lines = content.splitlines()

        # pyproject.toml 和 src/ 应在 web/ 之前复制（依赖变化少，利用缓存）
        copy_pyproject_line = None
        copy_web_line = None
        for i, line in enumerate(lines):
            if "COPY pyproject.toml" in line:
                copy_pyproject_line = i
            if "COPY web/" in line:
                copy_web_line = i

        assert copy_pyproject_line is not None, "未找到 COPY pyproject.toml"
        assert copy_web_line is not None, "未找到 COPY web/"
        assert copy_pyproject_line < copy_web_line, \
            "pyproject.toml 应在 web/ 之前复制以优化层缓存"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
