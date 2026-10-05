"""Cloud privacy regressions; all network access is mocked."""
import base64
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from service import cloud


class CloudPrivacyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root_patch = patch.object(cloud, "ROOT", Path(temp.name))
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.cfg = SimpleNamespace(config={"storage": {"cloud": {
            "namespace": "test-user", "chat_repo": "test-user/paper-chat-logs"}}})

    def test_existing_public_and_unknown_chat_repos_block_upload(self):
        for visibility in ("public", 5, 3, None, True):
            with self.subTest(visibility=visibility), \
                    patch.object(cloud, "_ctx", return_value=(self.cfg, "test-token")), \
                    patch("modelscope.hub.api.HubApi") as api, \
                    patch.object(cloud, "_sync_repo") as sync:
                api.return_value.get_repo.return_value = SimpleNamespace(visibility=visibility)
                self.assertFalse(cloud.push_chat_logs())
                sync.assert_not_called()

    def test_private_visibility_is_rechecked_for_each_upload(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(cloud, "ROOT", Path(tmp)), \
                patch.object(cloud, "_ctx", return_value=(self.cfg, "test-token")), \
                patch("modelscope.hub.api.HubApi") as api, \
                patch.object(cloud, "_git", return_value=SimpleNamespace(returncode=0)), \
                patch.object(cloud, "_sync_repo", return_value=True) as sync:
            api.return_value.get_repo.side_effect = [
                SimpleNamespace(visibility=1), SimpleNamespace(visibility=5)]
            self.assertTrue(cloud.push_chat_logs())
            self.assertFalse(cloud.push_chat_logs())
            self.assertEqual(sync.call_count, 1)

    def test_gated_access_is_not_proof_of_private_visibility(self):
        with patch.object(cloud, "_ctx", return_value=(self.cfg, "test-token")), \
                patch("modelscope.hub.api.HubApi") as api, \
                patch.object(cloud, "_sync_repo") as sync:
            api.return_value.get_repo.return_value = SimpleNamespace(visibility=1, gated=True, private=None)
            self.assertFalse(cloud.push_chat_logs())
            sync.assert_not_called()

    def test_lookup_or_creation_failure_blocks_upload_and_redacts_token(self):
        for method in ("create_repo", "get_repo"):
            with self.subTest(method=method), \
                    patch.object(cloud, "_ctx", return_value=(self.cfg, "test-token")), \
                    patch("modelscope.hub.api.HubApi") as api, \
                    patch.object(cloud, "_sync_repo") as sync, redirect_stdout(io.StringIO()) as out:
                getattr(api.return_value, method).side_effect = RuntimeError("test-token")
                self.assertFalse(cloud.push_chat_logs())
                sync.assert_not_called()
                self.assertNotIn("test-token", out.getvalue())

    def test_authentication_is_scoped_and_only_in_child_environment(self):
        before = dict(os.environ)
        with patch.object(cloud.subprocess, "run") as run:
            cloud._git(Path("temp"), "push", "origin", "master", token="test-token")
            args, kwargs = run.call_args
            self.assertNotIn("test-token", repr(args))
            env = kwargs["env"]
            start = int(before.get("GIT_CONFIG_COUNT", "0"))
            self.assertEqual(env[f"GIT_CONFIG_KEY_{start}"],
                             "http.https://www.modelscope.cn/.extraHeader")
            encoded = base64.b64encode(b"oauth2:test-token").decode()
            self.assertEqual(env[f"GIT_CONFIG_VALUE_{start}"], f"Authorization: Basic {encoded}")
        self.assertEqual(dict(os.environ), before)

    def test_legacy_fetch_and_push_credentials_removed_from_real_git_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
            for key in ("remote.origin.url", "remote.origin.pushurl"):
                cloud._git(root, "config", key, "https://oauth2:old-secret@www.modelscope.cn/datasets/u/r.git")
            cloud._ensure_remote(root, "test-user", "papers", "test-token")
            config = (root / ".git/config").read_text()
            self.assertNotIn("old-secret", config)
            self.assertNotIn("test-token", config)
            self.assertEqual(config.count(cloud._remote_url("test-user", "papers")), 2)

    def test_error_output_redacts_credentials(self):
        encoded = base64.b64encode(b"oauth2:test-token").decode()
        output = cloud._safe_output(f"test-token {encoded} https://oauth2:another-secret@host/repo", "test-token")
        for secret in ("test-token", encoded, "another-secret"):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
