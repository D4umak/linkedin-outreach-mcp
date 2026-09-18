"""Local-only product agent — patch this git checkout, optionally open a PR.

Never hooked from LinkedIn send paths. Cloud workers and installs without
a HeyLead git checkout stay off.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..ai.agent_loop import AgentBudget, AgentResult, run_agent_loop
from ..ai.product_agent import PRODUCT_AGENT_SYSTEM, build_product_context
from ..ai.schemas import PRODUCT_AGENT_STEP
from ..constants import (
    PRODUCT_AGENT_MAX_DIFF_LINES,
    PRODUCT_AGENT_MAX_FILES,
    PRODUCT_AGENT_MAX_STEPS,
    PRODUCT_AGENT_MAX_TOKENS,
    PRODUCT_AGENT_RESULT_CHARS,
    PRODUCT_AGENT_TIMEOUT_SECONDS,
    PRODUCT_AGENT_VALID_DECISIONS,
)
from ..db.async_bridge import run_db
from ..db.queries import get_setting, log_action
from ..flags import flag_enabled
from .agent_commons import async_commons_tools
from .coordinator import after_sibling_loop

logger = logging.getLogger(__name__)

ProductMode = Literal["off", "observe", "act"]
_DENIED_NAMES = frozenset({".env", ".env.local", "credentials.json"})
_DENIED_PARTS = frozenset({".git", ".heylead"})
_READ_FILE_CHARS = 8000
_CLOUD_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass
class ProductAgentOutcome:
    decision: str
    reason: str = ""
    applied: bool = False
    summary: str = ""
    draft_path: str = ""


def cloud_worker_locked() -> bool:
    raw = (os.environ.get("HEYLEAD_CLOUD_WORKER") or "").strip().lower()
    return raw in _CLOUD_TRUTHY


def resolve_product_repo(start: str | None = None) -> Path | None:
    """Return the HeyLead git checkout, or None if the gate fails."""
    env = (os.environ.get("HEYLEAD_REPO") or "").strip()
    cursor = Path(env or start or os.getcwd()).expanduser().resolve()
    for _ in range(8):
        if _is_heylead_checkout(cursor):
            return cursor
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return None


def _is_heylead_checkout(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    if not (root / "src" / "heylead").is_dir():
        return False
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "heylead" in text.lower():
            return True
    return True


def product_agent_mode(
    config: dict[str, Any] | None = None,
    *,
    repo: Path | None | str = "",
    skip_gate: bool = False,
) -> ProductMode:
    if not skip_gate:
        if cloud_worker_locked():
            return "off"
        if repo == "":
            repo = resolve_product_repo()
        if repo is None:
            return "off"
    cfg = config or {}
    raw_mode = cfg.get("product_agent_mode")
    if isinstance(raw_mode, str) and raw_mode.strip():
        text = raw_mode.strip().lower()
        if text in {"off", "observe", "act"}:
            return text  # type: ignore[return-value]
        if text in {"on", "true", "yes", "1"}:
            return "act"
        if text in {"false", "no", "0"}:
            return "off"
    if "enable_product_agent" in cfg and cfg.get("enable_product_agent") not in (None, ""):
        return "act" if flag_enabled(cfg, "enable_product_agent", default=False) else "off"
    return "act"


def deny_reason(rel: str) -> str:
    text = (rel or "").strip()
    if not text:
        return "empty path"
    if text.startswith("/") or text.startswith("~"):
        return "absolute path"
    parts = Path(text).parts
    if ".." in parts:
        return "parent traversal"
    if any(part in _DENIED_PARTS for part in parts):
        return "denied directory"
    if Path(text).name.lower() in _DENIED_NAMES:
        return "secret file"
    return ""


def paths_from_diff(diff: str) -> list[str]:
    found: list[str] = []
    for line in (diff or "").splitlines():
        if not line.startswith("+++ "):
            continue
        rest = line[4:].strip()
        if rest == "/dev/null":
            continue
        if rest.startswith("b/"):
            rest = rest[2:]
        if rest and rest not in found:
            found.append(rest)
    return found


def validate_patch(diff: str) -> str:
    text = (diff or "").strip()
    if not text:
        return "refused: empty patch"
    paths = paths_from_diff(text)
    if not paths:
        return "refused: no file paths in patch"
    if len(paths) > PRODUCT_AGENT_MAX_FILES:
        return f"refused: more than {PRODUCT_AGENT_MAX_FILES} files"
    added = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    if added > PRODUCT_AGENT_MAX_DIFF_LINES:
        return f"refused: more than {PRODUCT_AGENT_MAX_DIFF_LINES} added lines"
    for rel in paths:
        why = deny_reason(rel)
        if why:
            return f"refused: {rel} ({why})"
    return ""


def write_draft(repo: Path, diff: str) -> str:
    dest_dir = repo / ".agent" / "product"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time())}.diff"
    dest.write_text(diff, encoding="utf-8")
    return str(dest)


def apply_patch(repo: Path, diff: str) -> str:
    blocked = validate_patch(diff)
    if blocked:
        return blocked
    patch_path = repo / ".agent" / "product" / f"apply-{int(time.time())}.diff"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff, encoding="utf-8")
    check = subprocess.run(
        ["git", "apply", "--check", str(patch_path)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        return f"refused: git apply --check failed: {(check.stderr or check.stdout)[:240]}"
    applied = subprocess.run(
        ["git", "apply", str(patch_path)],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if applied.returncode != 0:
        return f"refused: git apply failed: {(applied.stderr or applied.stdout)[:240]}"
    return "ok"


def run_allowlisted_tests(repo: Path, paths: str) -> str:
    raw = [p.strip() for p in (paths or "").split(",") if p.strip()]
    if not raw:
        return "refused: empty test paths"
    safe: list[str] = []
    for rel in raw:
        why = deny_reason(rel)
        if why:
            return f"refused: {rel} ({why})"
        if not rel.startswith("tests/") or not rel.endswith(".py"):
            return f"refused: {rel} is not an allowlisted pytest path"
        if not (repo / rel).is_file():
            return f"refused: {rel} missing"
        safe.append(rel)
    result = subprocess.run(
        ["python3", "-m", "pytest", *safe, "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return f"failed:\n{output[:800]}"
    return f"ok\n{output[:400]}"


def open_pull_request(repo: Path, title: str, body: str, files: list[str]) -> str:
    headline = (title or "product agent change").strip()[:72]
    branch = f"product/{int(time.time())}"
    if subprocess.run(["git", "checkout", "-b", branch], cwd=repo, capture_output=True).returncode:
        return "refused: could not create branch"
    for rel in files:
        why = deny_reason(rel)
        if why:
            return f"refused: {rel} ({why})"
        add = subprocess.run(["git", "add", "--", rel], cwd=repo, capture_output=True, text=True)
        if add.returncode != 0:
            return f"refused: git add {rel}"
    commit = subprocess.run(
        ["git", "commit", "-m", headline],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        return f"refused: git commit failed: {(commit.stderr or commit.stdout)[:240]}"
    created = subprocess.run(
        ["gh", "pr", "create", "--title", headline, "--body", (body or headline)[:2000]],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        return f"refused: gh pr create failed: {(created.stderr or created.stdout)[:240]}"
    return (created.stdout or "ok").strip()


async def maybe_run_product_agent(
    request: str,
    *,
    config: dict[str, Any] | None = None,
    call_llm_fn: Any = None,
    resolve_repo_fn: Callable[[], Path | None] | None = None,
    apply_fn: Callable[[Path, str], str] | None = None,
    test_fn: Callable[[Path, str], str] | None = None,
    pr_fn: Callable[[Path, str, str, list[str]], str] | None = None,
) -> ProductAgentOutcome:
    """One bounded product tick. Failure → hold. Off → refuse, no beat."""
    try:
        repo = (resolve_repo_fn or resolve_product_repo)()
        cfg = config if config is not None else await _load_config()
        mode = product_agent_mode(cfg, repo=repo)
        if mode == "off":
            return ProductAgentOutcome(
                decision="none",
                reason="agent off",
                summary="off — no HeyLead git checkout, or cloud worker",
            )

        context = build_product_context(request=request, repo=str(repo or ""))
        tools = _build_tools(repo) if repo else {}
        tools.update(async_commons_tools(agent="product", campaign_id=""))
        result = await run_agent_loop(
            system=PRODUCT_AGENT_SYSTEM,
            context=context,
            tools=tools,
            schema=PRODUCT_AGENT_STEP,
            budget=AgentBudget(
                max_steps=PRODUCT_AGENT_MAX_STEPS,
                max_tokens=PRODUCT_AGENT_MAX_TOKENS,
                timeout_seconds=float(PRODUCT_AGENT_TIMEOUT_SECONDS),
                result_chars=PRODUCT_AGENT_RESULT_CHARS,
            ),
            call_llm_fn=call_llm_fn,
            valid_decisions=PRODUCT_AGENT_VALID_DECISIONS,
        )
        await after_sibling_loop(
            agent="product",
            campaign_id="",
            decision=result.decision,
            reason=result.reason,
            config=cfg,
        )
        await run_db(
            log_action, "product_agent_decision",
            result=result.decision,
            details={"reason": result.reason, "mode": mode},
        )
        return _apply_decision(
            result,
            mode=mode,
            repo=repo,
            apply_fn=apply_fn or apply_patch,
            test_fn=test_fn or run_allowlisted_tests,
            pr_fn=pr_fn or open_pull_request,
        )
    except Exception as exc:
        logger.warning("product agent failed: %s", exc)
        return ProductAgentOutcome(
            decision="hold",
            reason=str(exc)[:240],
            summary="hold — product agent could not run",
        )


def _apply_decision(
    result: AgentResult,
    *,
    mode: str,
    repo: Path | None,
    apply_fn: Callable[[Path, str], str],
    test_fn: Callable[[Path, str], str],
    pr_fn: Callable[[Path, str, str, list[str]], str],
) -> ProductAgentOutcome:
    decision = result.decision
    reason = result.reason or decision
    patch = result.extras.get("patch") or ""
    if not patch.strip():
        patch = ""
    if decision in {"none", "hold"} or not repo:
        return ProductAgentOutcome(
            decision=decision if decision in PRODUCT_AGENT_VALID_DECISIONS else "none",
            reason=reason,
            summary=f"{decision} — {reason}",
        )
    if mode == "observe" or decision == "draft":
        if not patch:
            return ProductAgentOutcome(
                decision="draft",
                reason=reason or "empty patch",
                summary="draft — no patch",
            )
        dest = write_draft(repo, patch)
        return ProductAgentOutcome(
            decision="draft",
            reason=reason,
            draft_path=dest,
            summary=f"draft written to {dest}",
        )
    if decision in {"apply", "pr"}:
        applied = apply_fn(repo, patch)
        if applied != "ok":
            return ProductAgentOutcome(
                decision="hold",
                reason=applied,
                summary=applied,
            )
        paths = (result.extras.get("paths") or "").strip()
        if paths:
            tested = test_fn(repo, paths)
            if not tested.startswith("ok"):
                return ProductAgentOutcome(
                    decision="hold",
                    reason=tested,
                    applied=True,
                    summary=tested,
                )
        if decision == "pr":
            opened = pr_fn(
                repo,
                result.extras.get("pr_title") or reason,
                result.extras.get("pr_body") or reason,
                paths_from_diff(patch),
            )
            if not opened or opened.startswith("refused"):
                return ProductAgentOutcome(
                    decision="hold",
                    reason=opened or "pr failed",
                    applied=True,
                    summary=opened or "pr failed",
                )
            return ProductAgentOutcome(
                decision="pr",
                reason=reason,
                applied=True,
                summary=opened,
            )
        return ProductAgentOutcome(
            decision="apply",
            reason=reason,
            applied=True,
            summary="applied",
        )
    return ProductAgentOutcome(decision=decision, reason=reason, summary=reason)


async def _load_config() -> dict[str, Any]:
    return {
        "product_agent_mode": await run_db(get_setting, "product_agent_mode", "") or "",
        "enable_product_agent": await run_db(get_setting, "enable_product_agent", "") or "",
    }


def _build_tools(repo: Path | None) -> dict[str, Callable[..., str]]:
    root = repo

    def read_file(path: str = "") -> str:
        if root is None:
            return "refused: no repo"
        why = deny_reason(path)
        if why:
            return f"refused: {why}"
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return "refused: path escapes repo"
        if not target.is_file():
            return "refused: missing file"
        return target.read_text(encoding="utf-8", errors="replace")[:_READ_FILE_CHARS]

    def search_repo(query: str = "") -> str:
        if root is None:
            return "refused: no repo"
        needle = (query or "").strip()
        if not needle:
            return "refused: empty query"
        result = subprocess.run(
            ["rg", "-n", "--max-count", "20", needle],
            cwd=root,
            capture_output=True,
            text=True,
        )
        return (result.stdout or result.stderr or "(no matches)")[:2000]

    def git_status() -> str:
        if root is None:
            return "refused: no repo"
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        return (result.stdout or result.stderr or "(clean)")[:1500]

    return {
        "read_file": read_file,
        "search_repo": search_repo,
        "git_status": git_status,
    }
