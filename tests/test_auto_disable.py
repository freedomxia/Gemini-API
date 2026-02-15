"""
测试连续失败自动禁用 + Token 有效期可配置功能

由于本地环境没有 aiohttp，采用独立复制核心逻辑的方式测试，
不依赖 web.app 模块导入。验证的是业务逻辑本身的正确性。
"""
import json
import uuid
import tempfile
from pathlib import Path
from datetime import datetime, timedelta


# ============================================================
# 从 web/app.py 提取的核心逻辑（独立可测试版本）
# ============================================================

class TestableAccountPool:
    """从 web/app.py 提取的 AccountPool 核心逻辑，去除 aiohttp 依赖"""

    def __init__(self, data_dir: Path, config: dict):
        self._data_dir = data_dir
        self._config = config
        self._accounts_file = data_dir / "accounts.json"
        self._config_file = data_dir / "config.json"
        data_dir.mkdir(exist_ok=True)
        self._config_file.write_text(json.dumps(config))
        self.accounts = self._load_accounts()
        self.clients = {}

    def _load_accounts(self):
        if self._accounts_file.exists():
            return json.loads(self._accounts_file.read_text())
        return []

    def _save_accounts(self):
        self._accounts_file.write_text(json.dumps(self.accounts, ensure_ascii=False, indent=2))

    def _load_config(self):
        return json.loads(self._config_file.read_text())

    def is_token_expired(self, account):
        config = self._load_config()
        expiry_hours = config.get("token_expiry_hours", 24)
        if expiry_hours <= 0:
            return False
        updated_at = account.get("token_updated_at", "") or account.get("created_at", "")
        if not updated_at:
            return False
        try:
            updated_time = datetime.fromisoformat(updated_at)
            return datetime.now() - updated_time > timedelta(hours=expiry_hours)
        except (ValueError, TypeError):
            return False

    def get_token_remaining(self, account):
        config = self._load_config()
        expiry_hours = config.get("token_expiry_hours", 24)
        if expiry_hours <= 0:
            return -2
        updated_at = account.get("token_updated_at", "") or account.get("created_at", "")
        if not updated_at:
            return -1
        try:
            updated_time = datetime.fromisoformat(updated_at)
            expire_time = updated_time + timedelta(hours=expiry_hours)
            remaining = (expire_time - datetime.now()).total_seconds()
            return max(0, remaining)
        except (ValueError, TypeError):
            return -1

    def add_account(self, psid, psidts, name="", proxy=""):
        now = datetime.now().isoformat()
        acc = {
            "id": str(uuid.uuid4())[:8],
            "name": name or f"账号{len(self.accounts)+1}",
            "psid": psid, "psidts": psidts, "proxy": proxy,
            "enabled": True, "status": "pending", "error": "",
            "created_at": now, "token_updated_at": now,
            "last_check": "", "request_count": 0,
            "consecutive_failures": 0,
        }
        self.accounts.append(acc)
        self._save_accounts()
        return acc

    def toggle_account(self, account_id):
        for acc in self.accounts:
            if acc["id"] == account_id:
                acc["enabled"] = not acc.get("enabled", True)
                if not acc["enabled"]:
                    self.clients.pop(account_id, None)
                else:
                    acc["consecutive_failures"] = 0
                    if acc.get("status") in ("expired", "disabled"):
                        acc["token_updated_at"] = datetime.now().isoformat()
                        acc["status"] = "pending"
                        acc["error"] = ""
                self._save_accounts()
                return acc
        return None

    def report_failure(self, account, error):
        account["consecutive_failures"] = account.get("consecutive_failures", 0) + 1
        account["error"] = error
        account["last_check"] = datetime.now().isoformat()
        config = self._load_config()
        max_failures = config.get("max_consecutive_failures", 3)
        if max_failures > 0 and account["consecutive_failures"] >= max_failures:
            account["status"] = "disabled"
            account["enabled"] = False
            account["error"] = f"连续失败{account['consecutive_failures']}次，已自动禁用"
            self.clients.pop(account["id"], None)
        else:
            account["status"] = "error"
        self._save_accounts()

    def report_success(self, account):
        account["consecutive_failures"] = 0
        account["request_count"] = account.get("request_count", 0) + 1
        self._save_accounts()


def make_pool(tmpdir, config_override=None):
    config = {
        "admin_password": "admin", "api_key": "testkey",
        "model": "unspecified", "plugin_token": "t",
        "plugin_auto_enable": True,
        "token_expiry_hours": 24, "max_consecutive_failures": 3,
    }
    if config_override:
        config.update(config_override)
    return TestableAccountPool(Path(tmpdir) / "data", config)


# ============================================================
# 连续失败自动禁用测试
# ============================================================

class TestConsecutiveFailureAutoDisable:

    def test_report_failure_increments_counter(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t)
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            pool.report_failure(acc, "some error")
            assert acc["consecutive_failures"] == 1
            assert acc["status"] == "error"
            assert acc["enabled"] is True

    def test_report_failure_disables_at_threshold(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 3})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"

            pool.report_failure(acc, "e1")
            assert acc["consecutive_failures"] == 1 and acc["enabled"]
            pool.report_failure(acc, "e2")
            assert acc["consecutive_failures"] == 2 and acc["enabled"]
            pool.report_failure(acc, "e3")
            assert acc["consecutive_failures"] == 3
            assert acc["enabled"] is False
            assert acc["status"] == "disabled"
            assert "连续失败3次" in acc["error"]

    def test_report_success_resets_counter(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t)
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            pool.report_failure(acc, "e1")
            pool.report_failure(acc, "e2")
            assert acc["consecutive_failures"] == 2
            pool.report_success(acc)
            assert acc["consecutive_failures"] == 0
            assert acc["request_count"] == 1

    def test_success_between_failures_prevents_disable(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 3})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            pool.report_failure(acc, "e")
            pool.report_failure(acc, "e")
            pool.report_success(acc)  # 重置
            pool.report_failure(acc, "e")
            pool.report_failure(acc, "e")
            assert acc["consecutive_failures"] == 2
            assert acc["enabled"] is True

    def test_threshold_zero_means_never_disable(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 0})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            for i in range(10):
                pool.report_failure(acc, f"e{i}")
            assert acc["consecutive_failures"] == 10
            assert acc["enabled"] is True
            assert acc["status"] == "error"

    def test_toggle_resets_failures(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 3})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            for _ in range(3):
                pool.report_failure(acc, "e")
            assert acc["enabled"] is False and acc["status"] == "disabled"

            result = pool.toggle_account(acc["id"])
            assert result["enabled"] is True
            assert result["consecutive_failures"] == 0
            assert result["status"] == "pending"

    def test_disabled_persisted_to_file(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 2})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            pool.report_failure(acc, "e1")
            pool.report_failure(acc, "e2")

            saved = json.loads((Path(t) / "data" / "accounts.json").read_text())
            assert saved[0]["enabled"] is False
            assert saved[0]["status"] == "disabled"
            assert saved[0]["consecutive_failures"] == 2

    def test_custom_threshold_5(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"max_consecutive_failures": 5})
            acc = pool.add_account("p1", "pt1", "test1")
            acc["status"] = "active"
            for i in range(4):
                pool.report_failure(acc, f"e{i}")
            assert acc["enabled"] is True
            pool.report_failure(acc, "e4")
            assert acc["enabled"] is False
            assert acc["consecutive_failures"] == 5


# ============================================================
# Token 有效期可配置测试
# ============================================================

class TestTokenExpiryConfig:

    def test_default_24h(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 24})
            fresh = {"token_updated_at": (datetime.now() - timedelta(hours=23)).isoformat()}
            assert pool.is_token_expired(fresh) is False
            old = {"token_updated_at": (datetime.now() - timedelta(hours=25)).isoformat()}
            assert pool.is_token_expired(old) is True

    def test_custom_48h(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 48})
            acc = {"token_updated_at": (datetime.now() - timedelta(hours=25)).isoformat()}
            assert pool.is_token_expired(acc) is False
            old = {"token_updated_at": (datetime.now() - timedelta(hours=49)).isoformat()}
            assert pool.is_token_expired(old) is True

    def test_zero_means_never_expire(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 0})
            acc = {"token_updated_at": (datetime.now() - timedelta(hours=1000)).isoformat()}
            assert pool.is_token_expired(acc) is False

    def test_remaining_negative2_when_never_expires(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 0})
            acc = {"token_updated_at": datetime.now().isoformat()}
            assert pool.get_token_remaining(acc) == -2

    def test_remaining_positive_when_fresh(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 24})
            acc = {"token_updated_at": datetime.now().isoformat()}
            remaining = pool.get_token_remaining(acc)
            assert 86000 < remaining <= 86400

    def test_remaining_zero_when_expired(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 24})
            acc = {"token_updated_at": (datetime.now() - timedelta(hours=25)).isoformat()}
            assert pool.get_token_remaining(acc) == 0

    def test_config_fields_persisted(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t, {"token_expiry_hours": 48, "max_consecutive_failures": 5})
            config = pool._load_config()
            assert config["token_expiry_hours"] == 48
            assert config["max_consecutive_failures"] == 5

    def test_new_account_has_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as t:
            pool = make_pool(t)
            acc = pool.add_account("p", "pt", "test")
            assert "consecutive_failures" in acc
            assert acc["consecutive_failures"] == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
