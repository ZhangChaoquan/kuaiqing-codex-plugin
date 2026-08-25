from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "plugins" / "kuaiqing" / "scripts" / "skill_manager.py"
SPEC = importlib.util.spec_from_file_location("skill_manager", SCRIPT)
sm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(sm)


class SkillManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.home_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.private = self.home / "private.pem"
        self.public_der = self.home / "public.der"
        self.openssl = sm.openssl_binary()
        subprocess.run([self.openssl, "genpkey", "-algorithm", "Ed25519", "-out", self.private], check=True)
        subprocess.run(
            [self.openssl, "pkey", "-in", self.private, "-pubout", "-outform", "DER", "-out", self.public_der],
            check=True,
        )
        self.public_patch = mock.patch.object(sm, "PUBLIC_KEY_B64", base64.b64encode(self.public_der.read_bytes()[-32:]).decode())
        self.public_patch.start()

    def tearDown(self) -> None:
        self.public_patch.stop()
        self.home_patch.stop()
        self.temp.cleanup()

    def package(self, files: dict[str, bytes] | None = None) -> bytes:
        files = files or {
            "SKILL.md": b"---\nname: kuaiqingweb-com\ndescription: OAuth account tools.\n---\n\n# Account\n"
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in files.items():
                archive.writestr(path, content)
        return output.getvalue()

    def item(self, package: bytes, version: str = "0.1.0") -> dict:
        item = {
            "id": "kuaiqingweb-com",
            "name": "KuaiQing",
            "version": version,
            "minimum_plugin_version": "0.1.1",
            "mcp_contract_version": 1,
            "release_notes": "OAuth only",
            "published_at": "2026-08-25T10:00:00+00:00",
            "package_url": "https://skills.kuaiqing.com/release.zip",
            "sha256": hashlib.sha256(package).hexdigest(),
            "signature_algorithm": "Ed25519",
        }
        manifest = sm.canonical_manifest(item)
        manifest_path = self.home / "manifest.json"
        signature_path = self.home / "signature.bin"
        manifest_path.write_bytes(manifest)
        subprocess.run(
            [self.openssl, "pkeyutl", "-sign", "-inkey", self.private, "-rawin", "-in", manifest_path, "-out", signature_path],
            check=True,
        )
        item["signature"] = base64.b64encode(signature_path.read_bytes()).decode()
        return item

    def install(self, item: dict, package: bytes) -> None:
        with mock.patch.object(sm, "fetch_catalog", return_value={"catalog_version": 1, "skills": [item]}), \
             mock.patch.object(sm, "fetch_bytes", return_value=package), \
             mock.patch.object(sm, "plugin_version", return_value="0.1.1"):
            sm.install(item["id"], item["version"], True)

    def test_signed_install_update_and_single_rollback(self) -> None:
        package_010 = self.package()
        self.install(self.item(package_010), package_010)
        self.assertTrue((sm.skills_root() / "kuaiqingweb-com" / "SKILL.md").exists())

        package_011 = self.package({
            "SKILL.md": b"---\nname: kuaiqingweb-com\ndescription: OAuth account tools.\n---\n\n# 0.1.1\n"
        })
        self.install(self.item(package_011, "0.1.1"), package_011)
        self.assertEqual(sm.load_registry()["skills"]["kuaiqingweb-com"]["version"], "0.1.1")
        sm.rollback("kuaiqingweb-com")
        self.assertEqual(sm.load_registry()["skills"]["kuaiqingweb-com"]["version"], "0.1.0")

    def test_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(sm.SkillManagerError):
            sm.install("kuaiqingweb-com", "0.1.0", False)
        with self.assertRaises(sm.SkillManagerError):
            sm.parse_semver("1.0.0-alpha.01")

    def test_network_failure_is_reported_without_raw_client_exception(self) -> None:
        with mock.patch.object(sm.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(sm.SkillManagerError, "下载失败"):
                sm.fetch_bytes("https://skills.example.test/catalog.json")

    def test_rejects_bad_signature_and_digest(self) -> None:
        package = self.package()
        item = self.item(package)
        item["signature"] = base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(sm.SkillManagerError):
            sm.verify_signature(item)
        item = self.item(package)
        item["sha256"] = "0" * 64
        with self.assertRaises(sm.SkillManagerError):
            self.install(item, package)

    def test_rejects_incompatible_plugin_and_mcp_contract(self) -> None:
        package = self.package()
        item = self.item(package)
        item["minimum_plugin_version"] = "9.0.0"
        with mock.patch.object(sm, "plugin_version", return_value="0.1.1"), self.assertRaises(sm.SkillManagerError):
            sm.validate_compatibility(item)
        item["minimum_plugin_version"] = "0.1.1"
        item["mcp_contract_version"] = 2
        with mock.patch.object(sm, "plugin_version", return_value="0.1.1"), self.assertRaises(sm.SkillManagerError):
            sm.validate_compatibility(item)

    def test_rejects_traversal_script_binary_and_symlink(self) -> None:
        bad_packages = [
            self.package({"SKILL.md": b"---\nname: kuaiqingweb-com\ndescription: x\n---\n", "../x.md": b"x"}),
            self.package({"SKILL.md": b"---\nname: kuaiqingweb-com\ndescription: x\n---\n", "templates/run.py": b"x"}),
            self.package({"SKILL.md": b"\xff"}),
        ]
        symlink = io.BytesIO()
        with zipfile.ZipFile(symlink, "w") as archive:
            archive.writestr("SKILL.md", b"---\nname: kuaiqingweb-com\ndescription: x\n---\n")
            info = zipfile.ZipInfo("references/link.md")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, b"target")
        bad_packages.append(symlink.getvalue())

        for index, package in enumerate(bad_packages):
            destination = self.home / f"unpack-{index}"
            destination.mkdir()
            with self.assertRaises(sm.SkillManagerError):
                sm.validate_and_unpack(package, "kuaiqingweb-com", destination)

    def test_failed_atomic_replace_restores_current_skill(self) -> None:
        package = self.package()
        self.install(self.item(package), package)
        current = (sm.skills_root() / "kuaiqingweb-com" / "SKILL.md").read_bytes()
        update = self.package({
            "SKILL.md": b"---\nname: kuaiqingweb-com\ndescription: OAuth account tools.\n---\n\n# new\n"
        })
        item = self.item(update, "0.1.1")
        real_replace = os.replace
        calls = 0

        def failing_replace(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated rename failure")
            return real_replace(source, target)

        with mock.patch.object(sm, "fetch_catalog", return_value={"catalog_version": 1, "skills": [item]}), \
             mock.patch.object(sm, "fetch_bytes", return_value=update), \
             mock.patch.object(sm, "plugin_version", return_value="0.1.1"), \
             mock.patch.object(sm.os, "replace", side_effect=failing_replace), \
             self.assertRaises(OSError):
            sm.install(item["id"], item["version"], True)
        self.assertEqual((sm.skills_root() / "kuaiqingweb-com" / "SKILL.md").read_bytes(), current)


if __name__ == "__main__":
    unittest.main()
