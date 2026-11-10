#!/usr/bin/env python3
"""Install signed KuaiQing Skills on macOS using only the Python stdlib and OpenSSL 3."""

from __future__ import annotations

import argparse
import base64
import hashlib
import fcntl
from contextlib import contextmanager
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

CATALOG_URL = "https://my.kuaiqingweb.com/api/v1/codex/skills/catalog"
PUBLIC_KEY_B64 = "wu+0j19Vg0KU1SoHnigHsgDYKOYl6OXt3MHeC0dP1Q8="
SUPPORTED_MCP_CONTRACT = 1
MAX_PACKAGE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024
MAX_ENTRY_BYTES = 512 * 1024
MAX_ENTRIES = 64
TEXT_EXTENSIONS = {".md", ".txt", ".json", ".yaml", ".yml", ".html", ".css", ".csv"}
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
MANIFEST_FIELDS = {
    "id",
    "name",
    "version",
    "minimum_plugin_version",
    "mcp_contract_version",
    "release_notes",
    "published_at",
    "package_url",
    "sha256",
    "signature_algorithm",
}


class SkillManagerError(RuntimeError):
    pass


def home_path() -> Path:
    return Path(os.environ.get("HOME", str(Path.home())))


def registry_path() -> Path:
    return home_path() / ".codex" / "kuaiqing" / "skills.json"


def skills_root() -> Path:
    return home_path() / ".codex" / "skills"


def rollback_root() -> Path:
    # ponytail: keeping rollback beside Skills guarantees same-filesystem rename on macOS.
    return skills_root() / ".kuaiqing-rollback"


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return
    raise SkillManagerError("Skill 目录和发布包必须使用 HTTPS。")


def fetch_bytes(url: str, limit: int = MAX_PACKAGE_BYTES) -> bytes:
    validate_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "KuaiQing-Skill-Manager/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            validate_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise SkillManagerError("下载内容超过大小限制。")
            data = response.read(limit + 1)
    except SkillManagerError:
        raise
    except (OSError, ValueError) as exc:
        raise SkillManagerError(f"下载失败：{exc}") from exc
    if len(data) > limit:
        raise SkillManagerError("下载内容超过大小限制。")
    return data


def fetch_catalog() -> dict[str, Any]:
    try:
        catalog = json.loads(fetch_bytes(CATALOG_URL, 1024 * 1024).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillManagerError("Skill 目录不是有效 UTF-8 JSON。") from exc
    if catalog.get("catalog_version") != 1 or not isinstance(catalog.get("skills"), list):
        raise SkillManagerError("不支持的 Skill 目录格式。")
    seen = set()
    for item in catalog["skills"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("version"), str):
            raise SkillManagerError("Skill 目录条目格式无效。")
        validate_skill_id(item["id"])
        parse_semver(item["version"])
        if item["id"] in seen:
            raise SkillManagerError("Skill 目录包含重复标识。")
        seen.add(item["id"])
    return catalog


def catalog_item(skill_id: str, expected_version: str | None = None) -> dict[str, Any]:
    for item in fetch_catalog()["skills"]:
        if isinstance(item, dict) and item.get("id") == skill_id:
            if expected_version is not None and item.get("version") != expected_version:
                raise SkillManagerError(
                    f"目录当前版本为 {item.get('version')}，与预期版本 {expected_version} 不一致。"
                )
            return item
    raise SkillManagerError(f"官方目录中没有启用的 Skill：{skill_id}")


def parse_semver(value: str) -> tuple[int, int, int, list[str] | None]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise SkillManagerError(f"非法 SemVer：{value}")
    prerelease = match.group(4)
    if prerelease and any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
        raise SkillManagerError(f"非法 SemVer：{value}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease.split(".") if prerelease else None


def compare_semver(left: str, right: str) -> int:
    left_major, left_minor, left_patch, left_pre = parse_semver(left)
    right_major, right_minor, right_patch, right_pre = parse_semver(right)
    base_left, base_right = (left_major, left_minor, left_patch), (right_major, right_minor, right_patch)
    if base_left != base_right:
        return 1 if base_left > base_right else -1
    if left_pre is None or right_pre is None:
        return 0 if left_pre is right_pre else (1 if left_pre is None else -1)
    for a, b in zip(left_pre, right_pre):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return 1 if int(a) > int(b) else -1
        if a.isdigit() != b.isdigit():
            return -1 if a.isdigit() else 1
        return 1 if a > b else -1
    return (len(left_pre) > len(right_pre)) - (len(left_pre) < len(right_pre))


def plugin_version() -> str:
    manifest = Path(__file__).resolve().parents[1] / ".codex-plugin" / "plugin.json"
    try:
        return json.loads(manifest.read_text("utf-8"))["version"].split("+", 1)[0]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise SkillManagerError("无法读取当前快擎插件版本。") from exc


def validate_compatibility(item: dict[str, Any]) -> None:
    minimum = item.get("minimum_plugin_version")
    if not isinstance(minimum, str) or compare_semver(plugin_version(), minimum) < 0:
        raise SkillManagerError(f"当前插件版本低于 Skill 要求的 {minimum}。")
    if item.get("mcp_contract_version") != SUPPORTED_MCP_CONTRACT:
        raise SkillManagerError("Skill 的 MCP 合同版本与当前插件不兼容。")


def canonical_manifest(item: dict[str, Any]) -> bytes:
    if item.get("signature_algorithm") != "Ed25519" or not MANIFEST_FIELDS.issubset(item):
        raise SkillManagerError("发布清单字段不完整或签名算法不受支持。")
    manifest = {key: item[key] for key in MANIFEST_FIELDS}
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def public_key_pem() -> bytes:
    raw = base64.b64decode(PUBLIC_KEY_B64, validate=True)
    if len(raw) != 32:
        raise SkillManagerError("插件内置验签公钥无效。")
    der = bytes.fromhex("302a300506032b6570032100") + raw
    encoded = base64.encodebytes(der).replace(b"\n", b"")
    lines = [encoded[index : index + 64] for index in range(0, len(encoded), 64)]
    return b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(lines) + b"\n-----END PUBLIC KEY-----\n"


def openssl_binary() -> str:
    candidates = [
        shutil.which("openssl"),
        "/opt/homebrew/opt/openssl@3/bin/openssl",
        "/opt/homebrew/bin/openssl",
        "/usr/local/opt/openssl@3/bin/openssl",
    ]
    for candidate in dict.fromkeys(filter(None, candidates)):
        if not Path(candidate).is_file():
            continue
        try:
            version = subprocess.run([candidate, "version"], capture_output=True, text=True, check=False)
        except OSError:
            continue
        match = re.search(r"OpenSSL\s+(\d+)", version.stdout)
        if version.returncode == 0 and match and int(match.group(1)) >= 3:
            return candidate
    raise SkillManagerError("需要 OpenSSL 3 才能验证 Skill 签名。")


def verify_signature(item: dict[str, Any]) -> None:
    try:
        signature = base64.b64decode(item["signature"], validate=True)
    except (KeyError, ValueError) as exc:
        raise SkillManagerError("发布签名无效。") from exc
    if len(signature) != 64:
        raise SkillManagerError("发布签名无效。")

    openssl = openssl_binary()

    with tempfile.TemporaryDirectory(prefix="kq-signature-") as directory:
        root = Path(directory)
        (root / "public.pem").write_bytes(public_key_pem())
        (root / "manifest.json").write_bytes(canonical_manifest(item))
        (root / "signature.bin").write_bytes(signature)
        result = subprocess.run(
            [
                openssl, "pkeyutl", "-verify", "-pubin", "-inkey", str(root / "public.pem"),
                "-rawin", "-in", str(root / "manifest.json"), "-sigfile", str(root / "signature.bin"),
            ],
            capture_output=True,
            check=False,
        )
    if result.returncode != 0:
        raise SkillManagerError("Skill 发布签名验证失败。")


def validate_entry(info: zipfile.ZipInfo) -> PurePosixPath | None:
    name = info.filename
    if "\x00" in name or "\\" in name:
        raise SkillManagerError("ZIP 包含非法路径。")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise SkillManagerError("ZIP 包含路径穿越或隐藏文件。")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise SkillManagerError("ZIP 包含软链接。")
    if info.is_dir():
        if not path.parts or path.parts[0] not in {"references", "templates"}:
            raise SkillManagerError("ZIP 包含不允许的目录。")
        return None
    if info.file_size > MAX_ENTRY_BYTES:
        raise SkillManagerError("ZIP 单个文件超过大小限制。")
    if info.file_size and (info.compress_size == 0 or info.file_size / info.compress_size > 100):
        raise SkillManagerError("ZIP 压缩比异常。")
    allowed = name == "SKILL.md" or (
        len(path.parts) >= 2 and path.parts[0] in {"references", "templates"} and path.suffix.lower() in TEXT_EXTENSIONS
    )
    if not allowed:
        raise SkillManagerError(f"ZIP 包含不允许的文件：{name}")
    return path


def validate_skill_markdown(content: str, skill_id: str) -> None:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
    if not match:
        raise SkillManagerError("SKILL.md 缺少合法 frontmatter。")
    name = re.search(r"(?m)^name:\s*([^\r\n]+)\s*$", match.group(1))
    description = re.search(r"(?m)^description:\s*(\S.*)$", match.group(1))
    if not name or name.group(1).strip(" '\"") != skill_id or not description:
        raise SkillManagerError("SKILL.md 的 name 或 description 无效。")


def validate_and_unpack(package: bytes, skill_id: str, destination: Path) -> None:
    archive_path = destination.parent / f".{skill_id}-download.zip"
    archive_path.write_bytes(package)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ENTRIES:
                raise SkillManagerError("ZIP 文件数量超过限制或为空。")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise SkillManagerError("ZIP 解压后内容超过大小限制。")
            seen: set[str] = set()
            for info in infos:
                path = validate_entry(info)
                if path is None:
                    continue
                if str(path) in seen:
                    raise SkillManagerError("ZIP 包含重复路径。")
                seen.add(str(path))
                try:
                    content = archive.read(info).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SkillManagerError(f"ZIP 文件不是 UTF-8 文本：{path}") from exc
                output = destination.joinpath(*path.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(content, encoding="utf-8", newline="")
            if "SKILL.md" not in seen:
                raise SkillManagerError("ZIP 缺少 SKILL.md。")
            validate_skill_markdown((destination / "SKILL.md").read_text("utf-8"), skill_id)
    except zipfile.BadZipFile as exc:
        raise SkillManagerError("下载内容不是有效 ZIP。") from exc
    finally:
        archive_path.unlink(missing_ok=True)


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"skills": {}}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillManagerError("本地 Skill 注册表损坏，已停止写入。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        raise SkillManagerError("本地 Skill 注册表格式无效，已停止写入。")
    return data


def save_registry(registry: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def validate_skill_id(skill_id: str) -> None:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill_id) or len(skill_id) > 64:
        raise SkillManagerError("Skill 标识无效。")


@contextmanager
def mutation_lock():
    path = registry_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SkillManagerError("另一个 Skill 更新正在执行，请稍后重试。") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def file_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink():
        raise SkillManagerError("Skill 目录不能是软链接。")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SkillManagerError("Skill 目录包含软链接。")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def install(skill_id: str, expected_version: str, confirmed: bool) -> None:
    with mutation_lock():
        _install(skill_id, expected_version, confirmed)


def _install(skill_id: str, expected_version: str, confirmed: bool, item: dict[str, Any] | None = None) -> None:
    if not confirmed:
        raise SkillManagerError("安装或更新前必须取得用户明确确认，并传入 --confirmed。")
    validate_skill_id(skill_id)
    parse_semver(expected_version)
    registry = load_registry()
    item = item or catalog_item(skill_id, expected_version)
    validate_compatibility(item)
    verify_signature(item)
    package = fetch_bytes(item["package_url"])
    if hashlib.sha256(package).hexdigest() != item.get("sha256"):
        raise SkillManagerError("Skill 包 SHA-256 校验失败。")

    root = skills_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{skill_id}-install-", dir=root))
    target = root / skill_id
    rollback = rollback_root() / skill_id
    backup = root / f".{skill_id}-backup"
    stale = root / f".{skill_id}-rollback-stale"
    moved_target = installed_new = moved_backup = moved_stale = False

    try:
        validate_and_unpack(package, skill_id, temporary)
        if backup.exists() or stale.exists():
            raise SkillManagerError("检测到未完成的旧更新，请先人工检查隐藏备份目录。")
        if rollback.exists():
            rollback.parent.mkdir(parents=True, exist_ok=True)
            os.replace(rollback, stale)
            moved_stale = True
        if target.exists():
            os.replace(target, backup)
            moved_target = True
        os.replace(temporary, target)
        installed_new = True
        if moved_target:
            rollback.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, rollback)
            moved_backup = True
        previous = registry["skills"].get(skill_id, {}).get("version") if moved_target else None
        registry["skills"][skill_id] = {
            "version": expected_version,
            "rollback_version": previous,
            "sha256": item["sha256"],
            "file_hashes": file_hashes(target),
        }
        save_registry(registry)
    except Exception:
        if installed_new and target.exists():
            shutil.rmtree(target)
        if moved_backup and rollback.exists():
            os.replace(rollback, target)
        elif moved_target and backup.exists():
            os.replace(backup, target)
        if moved_stale and stale.exists():
            rollback.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stale, rollback)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if moved_stale and stale.exists():
        shutil.rmtree(stale)


def rollback(skill_id: str) -> None:
    validate_skill_id(skill_id)
    with mutation_lock():
        _rollback(skill_id)


def _rollback(skill_id: str) -> None:
    registry = load_registry()
    state = registry["skills"].get(skill_id)
    if not isinstance(state, dict) or not state.get("rollback_version"):
        raise SkillManagerError("该 Skill 没有可回滚的本地版本。")
    target = skills_root() / skill_id
    previous = rollback_root() / skill_id
    swap = skills_root() / f".{skill_id}-rollback-swap"
    if not target.is_dir() or not previous.is_dir() or swap.exists():
        raise SkillManagerError("本地 Skill 或回滚副本状态不完整。")
    os.replace(target, swap)
    try:
        os.replace(previous, target)
        os.replace(swap, previous)
    except Exception:
        if target.exists() and not previous.exists():
            os.replace(target, previous)
        if swap.exists() and not target.exists():
            os.replace(swap, target)
        raise
    state["version"], state["rollback_version"] = state["rollback_version"], state["version"]
    state.pop("sha256", None)
    state["file_hashes"] = file_hashes(target)
    state["pinned_version"] = state["version"]
    save_registry(registry)


def sync() -> dict[str, Any]:
    """Maintain signed website Skills; never overwrite unmanaged or locally edited files."""
    with mutation_lock():
        catalog = fetch_catalog()
        registry = load_registry()["skills"]
        results = []
        for item in catalog["skills"]:
            skill_id = item.get("id", "")
            if skill_id == "kuaiqing-skill-release-management" or not (item.get("auto_install") is True or skill_id in registry):
                continue
            try:
                validate_skill_id(skill_id)
                validate_compatibility(item)
                verify_signature(item)
                target = skills_root() / skill_id
                state = registry.get(skill_id, {})
                if target.exists() and not state:
                    raise SkillManagerError("同名 Skill 不由官方管理器管理，已保留原目录。")
                if target.exists() and state:
                    expected_files = state.get("file_hashes")
                    if expected_files is None:
                        # Older registries only stored the ZIP digest. Recover its signed file snapshot.
                        version = state.get("version", "")
                        validate_skill_id(skill_id)
                        parse_semver(version)
                        parsed = urllib.parse.urlparse(CATALOG_URL)
                        previous_url = urllib.parse.urlunparse(parsed._replace(path=f"/api/v1/codex/skills/{skill_id}/{version}/download"))
                        previous = fetch_bytes(previous_url)
                        if hashlib.sha256(previous).hexdigest() != state.get("sha256"):
                            raise SkillManagerError("旧版安装记录无法验证，请显式检查后更新。")
                        with tempfile.TemporaryDirectory(prefix="kq-skill-baseline-") as directory:
                            baseline = Path(directory)
                            validate_and_unpack(previous, skill_id, baseline)
                            expected_files = file_hashes(baseline)
                    if file_hashes(target) != expected_files:
                        raise SkillManagerError("Skill 有本地修改，自动更新已停止并保留修改。")
                if state.get("pinned_version"):
                    if not target.is_dir():
                        raise SkillManagerError("固定版本的本地目录缺失，请显式检查后恢复。")
                    status = "pinned"
                elif state.get("version") and compare_semver(item["version"], state["version"]) < 0:
                    status = "rollback_available"
                elif state.get("version") == item["version"] and target.is_dir():
                    if state.get("sha256") and state["sha256"] != item["sha256"]:
                        raise SkillManagerError("相同版本摘要发生变化，拒绝覆盖不可变版本。")
                    status = "current"
                else:
                    _install(skill_id, item["version"], True, item)
                    status = "updated" if state else "installed"
                results.append({"id": skill_id, "status": status, "version": load_registry()["skills"][skill_id]["version"], "skill_path": str(target / "SKILL.md")})
            except (SkillManagerError, OSError, ValueError) as exc:
                results.append({"id": skill_id, "status": "failed", "message": str(exc)})
        if not results:
            raise SkillManagerError("目录未提供可自动安装的业务 Skill，请检查官方发布目录。")
        return {"success": all(item["status"] != "failed" for item in results), "skills": results,
                "load_instructions": "Read the returned SKILL.md for the requested website task in this chat; new tasks discover installed Skills automatically."}


def check(skill_id: str | None) -> dict[str, Any]:
    catalog = fetch_catalog()
    registry = load_registry()["skills"]
    items = [item for item in catalog["skills"] if skill_id is None or item.get("id") == skill_id]
    if skill_id is not None and not items:
        raise SkillManagerError(f"官方目录中没有启用的 Skill：{skill_id}")
    return {
        "skills": [
            {
                "id": item["id"],
                "installed_version": registry.get(item["id"], {}).get("version"),
                "available_version": item["version"],
                "update_available": registry.get(item["id"], {}).get("version") != item["version"],
                "release_notes": item.get("release_notes", ""),
            }
            for item in items
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="KuaiQing Skill Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("skill", nargs="?")
    for command in ("install", "update"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("skill")
        command_parser.add_argument("version")
        command_parser.add_argument("--confirmed", action="store_true")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("skill")
    args = parser.parse_args()

    try:
        if args.command == "sync":
            result = sync()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["success"] else 1
        if args.command == "check":
            print(json.dumps(check(args.skill), ensure_ascii=False, indent=2))
        elif args.command in {"install", "update"}:
            install(args.skill, args.version, args.confirmed)
            print(f"{args.skill} {args.version} 安装完成；请新建 Codex 任务加载新版本。")
        else:
            rollback(args.skill)
            print(f"{args.skill} 已回滚；请新建 Codex 任务加载该版本。")
        return 0
    except SkillManagerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
