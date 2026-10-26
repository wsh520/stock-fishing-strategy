"""凭据守卫（tools/pre_commit_guard.py）的判定回归。

守卫是「凭据不进 PUBLIC 仓库」的最后一道闸。它的两类失效都很致命：
  a) 该拦的没拦住 —— 真实密码被提交；
  b) 不该拦的拦了 —— 正常代码无法提交，用户被迫长期用 --no-verify，闸门形同虚设。
所以正例与反例必须同时锁定。

跑法（项目根）：
    PYTHONPATH=".venv/Lib/site-packages;." python tests/test_credential_guard.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pre_commit_guard import _path_problem, _scan_text  # noqa: E402


class TestMustHit(unittest.TestCase):
    """真实凭据必须命中。"""

    def test_password_quoted(self):
        self.assertTrue(_scan_text("t.env", "MYSQL_PASSWORD='Xk9dL2mQp7vN'"))

    def test_password_bare(self):
        self.assertTrue(_scan_text("t.env", "MYSQL_PASSWORD=Xk9dL2mQp7vN"))

    def test_export_form(self):
        self.assertTrue(_scan_text("t.env", "export MYSQL_PASSWORD='Xk9dL2mQp7vN'"))

    def test_yaml_colon_form(self):
        self.assertTrue(_scan_text("t.yml", "MYSQL_PASSWORD: Xk9dL2mQp7vN"))

    def test_host_and_user(self):
        self.assertTrue(_scan_text("t.env", "MYSQL_HOST='rm-abc.mysql.rds.aliyuncs.com'"))
        self.assertTrue(_scan_text("t.env", "MYSQL_USER='dbreader'"))

    def test_trailing_comment_still_hits(self):
        self.assertTrue(_scan_text("t.env", "MYSQL_PASSWORD='Xk9dL2mQp7vN'  # prod"))

    def test_private_key_block(self):
        # 样本在源码里用拼接构造，避免「守卫拦住自己的测试文件」。
        # 运行时拼成完整串后再喂给 _scan_text，检测能力不受影响。
        sample = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
        self.assertTrue(_scan_text("t.txt", sample))

    def test_feishu_webhook(self):
        url = ("https://open.feishu.cn/open-apis/bot/v2/hook/"
               "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d")
        self.assertTrue(_scan_text("t.yml", url))

    def test_cloud_ak(self):
        self.assertTrue(_scan_text("t.py", "ghp_" + "a" * 36))
        self.assertTrue(_scan_text("t.py", "LTAI" + "5tAbCdEfGhIjKlMn"))


class TestMustPass(unittest.TestCase):
    """模板与变量引用必须放行，否则正常开发被卡死。"""

    def test_empty_template(self):
        self.assertFalse(_scan_text("t.env", "MYSQL_PASSWORD=''"))

    def test_environ_reference(self):
        self.assertFalse(_scan_text("t.py", 'MYSQL_PASSWORD = os.environ["MYSQL_PASSWORD"]'))

    def test_getenv_reference(self):
        self.assertFalse(_scan_text("t.py", 'MYSQL_PASSWORD=getenv("MYSQL_PASSWORD")'))

    def test_github_secrets_injection(self):
        self.assertFalse(_scan_text("t.yml", "MYSQL_PASSWORD: ${{ secrets.MYSQL_PASSWORD }}"))

    def test_placeholders(self):
        self.assertFalse(_scan_text("t.env", "MYSQL_PASSWORD='your_password_here'"))
        self.assertFalse(_scan_text("t.env", "MYSQL_PASSWORD=<FILL_ME>"))

    def test_sql_placeholder(self):
        self.assertFalse(_scan_text("t.py", "password=%s"))

    def test_readme_mention(self):
        text = "通过环境变量配置（MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD）"
        self.assertFalse(_scan_text("README.md", text))


class TestPathRules(unittest.TestCase):
    def test_blocked_paths(self):
        for p in (".secrets/stock-db.env", ".env.local", "config/.env", "cert.pem",
                  "id_rsa", "store/credentials.json", "a/b/server.key"):
            with self.subTest(path=p):
                self.assertIsNotNone(_path_problem(p), f"应拦截 {p}")

    def test_allowed_paths(self):
        for p in (".env.example", "src/app.py", "docs/env_notes.md", ".gitignore"):
            with self.subTest(path=p):
                self.assertIsNone(_path_problem(p), f"应放行 {p}")

    def test_credential_named_code_allowed(self):
        """守卫不能拦住自己这类代码文件，否则用户被迫长期 --no-verify。"""
        for p in ("tools/pre_commit_guard.py", "tests/test_credential_guard.py"):
            with self.subTest(path=p):
                self.assertIsNone(_path_problem(p), f"代码文件应放行 {p}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
