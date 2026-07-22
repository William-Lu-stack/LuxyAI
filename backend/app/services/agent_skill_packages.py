"""Agent Skills 标准目录包的编解码与安全导入导出。

标准 ``SKILL.md`` 保持跨智能体可读；Flawless 专有的证据、动作和恢复门禁放在
``references/ops-policy.yaml``。平台不会直接执行导入包中的脚本，脚本执行仍只能
引用企业批准目录中的 ``script_id``。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import tempfile
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


AGENT_SKILL_SPEC = "agentskills.io/v1"
OPS_POLICY_SCHEMA = "flawless.io/ops-skill/v1"
MAX_PACKAGE_FILES = max(8, int(os.getenv("OPS_SKILL_MAX_PACKAGE_FILES", "128")))
MAX_PACKAGE_BYTES = max(64 * 1024, int(os.getenv("OPS_SKILL_MAX_PACKAGE_BYTES", str(8 * 1024 * 1024))))
MAX_TEXT_BYTES = 1024 * 1024
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)client_secret\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{12,}"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"),
)


class AgentSkillPackageError(ValueError):
    """Skill 包格式或安全检查失败。"""


def normalize_skill_name(value: str, *, fallback: str = "ops-skill") -> str:
    """转换为 Agent Skills 规范允许的目录名。"""
    raw = str(value or fallback).strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    raw = re.sub(r"-{2,}", "-", raw)[:64].rstrip("-")
    if not raw:
        raw = fallback
    if not SKILL_NAME_PATTERN.fullmatch(raw):
        raise AgentSkillPackageError("Skill name 必须只包含小写字母、数字和单连字符")
    return raw


def _description(skill: dict[str, Any]) -> str:
    summary = str(skill.get("summary") or skill.get("description") or "").strip()
    symptoms = [str(item).strip() for item in skill.get("symptoms") or [] if str(item).strip()]
    suffix = f" Use when signals include: {', '.join(symptoms[:8])}." if symptoms else ""
    value = (summary + suffix).strip()
    if not value:
        value = "Diagnose and remediate Kubernetes operations incidents with evidence and recovery verification."
    return value[:1024]


def _yaml_frontmatter(name: str, description: str) -> str:
    metadata = yaml.safe_dump(
        {"name": name, "description": description},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{metadata}\n---\n"


def render_skill_md(skill: dict[str, Any]) -> str:
    """生成兼容 Agent Skills 的主说明文件。"""
    name = normalize_skill_name(str(skill.get("id") or skill.get("name") or "ops-skill"))
    title = str(skill.get("name") or name).strip()
    summary = str(skill.get("summary") or _description(skill)).strip()
    symptoms = [str(item) for item in skill.get("symptoms") or []]
    evidence = [str(item) for item in skill.get("evidence_required") or []]
    steps = [str(item) for item in skill.get("diagnostic_steps") or []]
    actions = [str(item) for item in skill.get("allowed_actions") or []]
    criteria = [str(item) for item in skill.get("success_criteria") or []]
    rollback = str(skill.get("rollback") or "Use the recorded pre-change state or the platform-approved rollback action.").strip()

    def bullets(values: list[str], empty: str) -> str:
        return "\n".join(f"- {item}" for item in values) if values else f"- {empty}"

    workflow = "\n".join(f"{index}. {item}" for index, item in enumerate(steps, 1))
    if not workflow:
        workflow = "1. Collect direct runtime evidence before proposing any mutation.\n2. Produce a minimal, reversible plan and verify recovery."
    body = f"""
# {title}

## Objective

{summary}

## Trigger Signals

{bullets(symptoms, "Use the description and current incident evidence to determine applicability.")}

## Required Evidence

Collect and validate these evidence classes before proposing a change:

{bullets(evidence, "Collect logs, events, resource state, recent changes, and relevant metrics.")}

## Workflow

{workflow}

## Allowed Operations

Treat these as operation intents, not shell commands. Map them to the host agent's approved tools and permissions:

{bullets(actions, "Instruction-only Skill. Do not mutate infrastructure without an approved host action.")}

## Recovery Verification

Do not report success until the following observable conditions hold:

{bullets(criteria, "Verify workload health, error signals, and business-facing availability.")}

## Safety Contract

- Distinguish symptoms from root cause and cite the evidence supporting each conclusion.
- Preview the target, impact, diff, risk, and rollback before any mutation.
- Use the smallest reversible change and require human approval for production or high-risk actions.
- Stop when required evidence is missing, the target is outside scope, or recovery cannot be verified.
- Never invent credentials, storage paths, image tags, Secret values, or successful execution results.
- Roll back with: {rollback}

## Host Integration

Read `references/ops-policy.yaml` when the host supports Flawless structured evidence and action gates.
Imported scripts are not trusted automatically; use only scripts explicitly approved by the host platform.
""".strip()
    return _yaml_frontmatter(name, _description(skill)) + "\n" + body + "\n"


def render_ops_policy(skill: dict[str, Any]) -> str:
    """生成可选的 Flawless 机器可读扩展。"""
    payload = {
        "schema": OPS_POLICY_SCHEMA,
        "identity": {
            "id": normalize_skill_name(str(skill.get("id") or skill.get("name") or "ops-skill")),
            "display_name": str(skill.get("name") or "Ops Skill"),
            "summary": str(skill.get("summary") or skill.get("description") or ""),
            "version": str(skill.get("version") or "1.0.0"),
            "owner": str(skill.get("owner") or "operator"),
            "category": str(skill.get("category") or "custom"),
        },
        "matching": {
            "symptoms": list(skill.get("symptoms") or []),
            "applies_to": list(skill.get("applies_to") or []),
        },
        "workflow": {
            "evidence_required": list(skill.get("evidence_required") or []),
            "diagnostic_steps": list(skill.get("diagnostic_steps") or []),
            "allowed_actions": list(skill.get("allowed_actions") or []),
            "success_criteria": list(skill.get("success_criteria") or []),
        },
        "guardrails": {
            "risk": str(skill.get("risk") or "medium"),
            "rollback": str(skill.get("rollback") or ""),
            "human_confirmation": True,
            "arbitrary_shell": False,
            "script_policy": deepcopy(skill.get("script_policy") or {"enabled": False}),
        },
        "lifecycle": {
            "enabled": bool(skill.get("enabled", True)),
            "state": str(skill.get("lifecycle") or ("published" if skill.get("enabled", True) else "candidate")),
            "builtin": bool(skill.get("builtin", False)),
            "created_at": skill.get("created_at"),
            "updated_at": skill.get("updated_at"),
            "updated_by": skill.get("updated_by"),
        },
        "portability": {
            "format": AGENT_SKILL_SPEC,
            "host_action_mapping_required": True,
            "bundled_scripts_trusted": False,
        },
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)


def render_openai_yaml(skill: dict[str, Any]) -> str:
    name = normalize_skill_name(str(skill.get("id") or skill.get("name") or "ops-skill"))
    title = str(skill.get("name") or name)[:48]
    summary = str(skill.get("summary") or "基于证据完成诊断、受控变更和恢复验证")
    payload = {
        "interface": {
            "display_name": title,
            "short_description": summary[:64],
            "default_prompt": f"Use ${name} to diagnose this incident from evidence and propose a safe, verifiable remediation plan.",
        },
        "policy": {"allow_implicit_invocation": bool(skill.get("enabled", True))},
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _parse_skill_md(content: str, directory_name: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise AgentSkillPackageError("SKILL.md 必须以 YAML frontmatter 开始")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, flags=re.S)
    if not match:
        raise AgentSkillPackageError("SKILL.md frontmatter 未正确闭合")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise AgentSkillPackageError(f"SKILL.md frontmatter 不是有效 YAML：{exc}") from exc
    name = str(metadata.get("name") or "")
    description = str(metadata.get("description") or "").strip()
    if not SKILL_NAME_PATTERN.fullmatch(name) or name != directory_name:
        raise AgentSkillPackageError("SKILL.md name 必须符合规范并与父目录名称一致")
    if not description or len(description) > 1024:
        raise AgentSkillPackageError("SKILL.md description 必须为 1-1024 个字符")
    return {"name": name, "description": description, **metadata}, match.group(2).strip()


def _declared_skill_name(content: str) -> str:
    """读取根目录 ZIP 的声明名称，用于补齐被压缩软件剥离的顶层目录。"""
    match = re.match(r"^---\s*\n(.*?)\n---", content, flags=re.S)
    if not match:
        raise AgentSkillPackageError("SKILL.md frontmatter 未正确闭合")
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise AgentSkillPackageError(f"SKILL.md frontmatter 不是有效 YAML：{exc}") from exc
    name = str(metadata.get("name") or "")
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise AgentSkillPackageError("SKILL.md name 不符合 Agent Skills 规范")
    return name


def _load_policy(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise AgentSkillPackageError(f"ops-policy.yaml 无法读取：{exc}") from exc
    if not isinstance(value, dict):
        raise AgentSkillPackageError("ops-policy.yaml 必须是 YAML 对象")
    return value


def read_package(package_dir: Path) -> dict[str, Any]:
    """把标准目录包解析为运行时注册表记录。"""
    skill_md = package_dir / "SKILL.md"
    if not skill_md.is_file():
        raise AgentSkillPackageError(f"{package_dir.name} 缺少 SKILL.md")
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AgentSkillPackageError(f"SKILL.md 无法读取：{exc}") from exc
    metadata, body = _parse_skill_md(content, package_dir.name)
    policy = _load_policy(package_dir / "references" / "ops-policy.yaml")
    identity = policy.get("identity") or {}
    matching = policy.get("matching") or {}
    workflow = policy.get("workflow") or {}
    guardrails = policy.get("guardrails") or {}
    lifecycle = policy.get("lifecycle") or {}
    script_files = sorted(
        str(path.relative_to(package_dir))
        for path in (package_dir / "scripts").rglob("*")
        if path.is_file()
    ) if (package_dir / "scripts").is_dir() else []
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "id": metadata["name"],
        "name": identity.get("display_name") or metadata["name"],
        "description": metadata["description"],
        "summary": identity.get("summary") or metadata["description"],
        "instructions": body,
        "version": identity.get("version") or "1.0.0",
        "owner": identity.get("owner") or "imported",
        "category": identity.get("category") or "portable",
        "symptoms": list(matching.get("symptoms") or []),
        "applies_to": list(matching.get("applies_to") or []),
        "evidence_required": list(workflow.get("evidence_required") or []),
        "diagnostic_steps": list(workflow.get("diagnostic_steps") or []),
        "allowed_actions": list(workflow.get("allowed_actions") or []),
        "success_criteria": list(workflow.get("success_criteria") or []),
        "risk": guardrails.get("risk") or "high",
        "rollback": guardrails.get("rollback") or "",
        "script_policy": guardrails.get("script_policy") or {"enabled": False},
        "enabled": bool(lifecycle.get("enabled", True)),
        "lifecycle": str(lifecycle.get("state") or ("published" if lifecycle.get("enabled", True) else "candidate")),
        "builtin": bool(lifecycle.get("builtin", False)),
        "created_at": lifecycle.get("created_at"),
        "updated_at": lifecycle.get("updated_at"),
        "updated_by": lifecycle.get("updated_by") or "package-loader",
        "format": AGENT_SKILL_SPEC,
        "portable": True,
        "execution_ready": bool(policy and workflow.get("allowed_actions")),
        "package_path": str(package_dir),
        "package_files": sum(1 for path in package_dir.rglob("*") if path.is_file()),
        "bundled_scripts": script_files,
        "bundled_scripts_trusted": False,
        "checksum": checksum,
    }


def write_package(root: Path, skill: dict[str, Any]) -> Path:
    """原子写入一个标准 Skill 目录，并保留已有的额外资源文件。"""
    name = normalize_skill_name(str(skill.get("id") or skill.get("name") or "ops-skill"))
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if target.exists() and not target.is_dir():
        raise AgentSkillPackageError(f"Skill 目标路径不是目录：{target}")
    target.mkdir(parents=True, exist_ok=True)
    (target / "references").mkdir(exist_ok=True)
    (target / "agents").mkdir(exist_ok=True)
    files = {
        target / "SKILL.md": render_skill_md({**skill, "id": name}),
        target / "references" / "ops-policy.yaml": render_ops_policy({**skill, "id": name}),
        target / "agents" / "openai.yaml": render_openai_yaml({**skill, "id": name}),
    }
    for path, content in files.items():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return target


def delete_package(root: Path, skill_id: str) -> None:
    name = normalize_skill_name(skill_id)
    target = root / name
    if target.is_dir() and target.parent.resolve() == root.resolve():
        shutil.rmtree(target)


def export_package(root: Path, skill_id: str) -> tuple[str, bytes]:
    """导出单个目录包，ZIP 内保留顶层 Skill 文件夹。"""
    name = normalize_skill_name(skill_id)
    package_dir = root / name
    if not package_dir.is_dir():
        raise AgentSkillPackageError("Skill 目录包不存在")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(name) / path.relative_to(package_dir)).as_posix())
    return f"{name}.zip", output.getvalue()


def _safe_archive_member(info: zipfile.ZipInfo) -> PurePosixPath | None:
    path = PurePosixPath(info.filename.replace("\\", "/"))
    if not info.filename or info.is_dir() or path.name in {".DS_Store", "Thumbs.db"}:
        return None
    if path.is_absolute() or ".." in path.parts or any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
        raise AgentSkillPackageError(f"ZIP 包含不安全路径：{info.filename}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode and stat.S_ISLNK(mode):
        raise AgentSkillPackageError(f"ZIP 不允许符号链接：{info.filename}")
    return path


def _scan_text_secrets(filename: str, data: bytes) -> None:
    if len(data) > MAX_TEXT_BYTES or b"\x00" in data:
        return
    text = data.decode("utf-8", errors="ignore")
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise AgentSkillPackageError(f"{filename} 疑似包含凭据或私钥，请移除后再导入")


def import_archive(root: Path, filename: str, data: bytes) -> list[dict[str, Any]]:
    """安全导入一个 ZIP；允许其中包含一个或多个标准 Skill 顶层目录。"""
    if not filename.lower().endswith(".zip"):
        raise AgentSkillPackageError("请上传 .zip 格式的 Agent Skill 包")
    if not data or len(data) > MAX_PACKAGE_BYTES:
        raise AgentSkillPackageError(f"Skill ZIP 必须小于 {MAX_PACKAGE_BYTES // 1024 // 1024} MiB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AgentSkillPackageError("上传文件不是有效 ZIP") from exc
    with archive:
        members = [item for item in archive.infolist() if _safe_archive_member(item)]
        if len(members) > MAX_PACKAGE_FILES:
            raise AgentSkillPackageError(f"Skill ZIP 文件数不能超过 {MAX_PACKAGE_FILES}")
        if sum(item.file_size for item in members) > MAX_PACKAGE_BYTES:
            raise AgentSkillPackageError("Skill ZIP 解压后体积超过限制")
        with tempfile.TemporaryDirectory(prefix="flawless-skill-import-") as directory:
            base = Path(directory)
            staging = base / "extract"
            staging.mkdir()
            for info in members:
                path = _safe_archive_member(info)
                if path is None:
                    continue
                content = archive.read(info)
                _scan_text_secrets(info.filename, content)
                destination = staging.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            root_skill = staging / "SKILL.md"
            if root_skill.is_file():
                name = _declared_skill_name(root_skill.read_text(encoding="utf-8"))
                package_dir = base / "normalized" / name
                package_dir.mkdir(parents=True)
                for child in list(staging.iterdir()):
                    shutil.move(str(child), package_dir / child.name)
                skill_files = [package_dir / "SKILL.md"]
            else:
                skill_files = sorted(staging.rglob("SKILL.md"))
            if not skill_files:
                raise AgentSkillPackageError("ZIP 中未找到 SKILL.md")
            parsed: list[tuple[Path, dict[str, Any]]] = []
            for skill_file in skill_files:
                package_dir = skill_file.parent
                record = read_package(package_dir)
                parsed.append((package_dir, record))
            root.mkdir(parents=True, exist_ok=True)
            imported = []
            for source, record in parsed:
                destination = root / record["id"]
                temporary = root / f".{record['id']}.importing"
                if temporary.exists():
                    shutil.rmtree(temporary)
                shutil.copytree(source, temporary)
                if destination.exists():
                    shutil.rmtree(destination)
                temporary.replace(destination)
                imported.append(read_package(destination))
            return imported
