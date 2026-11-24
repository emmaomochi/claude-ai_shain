#!/usr/bin/env python3
"""原文と直した文の意味のズレ検出・自動復旧・読点整理をまとめて扱う CLI。

使い方:
    python3 nihongo/scripts/cli.py diff <orig> <draft> [--request <req>] [--profile <path>] [--scene {casual,work,formal}] [--json]
    python3 nihongo/scripts/cli.py fix <orig> <draft> <out> [--request <req>] [--no-trim-commas] [--fail-on-remaining]
    python3 nihongo/scripts/cli.py git [--base HEAD] [--staged] [--ext .md,.txt] [--request <req>] [--profile <path>] [--scene {casual,work,formal}] [--hook] [--json] [paths...]

標準ライブラリだけで動く。pyproject.toml からインストールした場合は `nihongo-check` として実行できる。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

__version__ = "3.0.0"

if __package__:
    from . import revert_broken, trim_commas
else:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    import revert_broken  # noqa: E402
    import trim_commas  # noqa: E402


def _ensure_utf8_stdio() -> None:
    """Windows 環境でも UTF-8 で安全に標準出力・標準エラー出力へ書けるようにする。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _read_utf8(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load_request(req_arg: str | None) -> str:
    """--request に渡された値を読み込む。既存ファイルなら中身を読み、そうでなければ文字列として扱う。"""
    if not req_arg:
        return ""
    try:
        if os.path.isfile(req_arg):
            return _read_utf8(req_arg)
    except OSError:
        pass
    return req_arg


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


_PROFILE_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_PROFILE_SCENE_RE = re.compile(r"\[(common|casual|work|formal)\]", re.IGNORECASE)
_FORBIDDEN_LINE_RE = re.compile(
    r"^\s*(?:[-*・]\s*)?(?:\*\*)?絶対に言わない表現(?:\*\*)?\s*[：:]\s*(.*)$"
)
_PLACEHOLDER_PAREN_RE = re.compile(
    r"[（(]\s*(?:例|記入例|未記入)\s*(?:[：:][^）)]*)?[）)]"
)
_PLACEHOLDER_PREFIX_RE = re.compile(r"^(?:例|記入例|未記入)\s*[：:]")
_FORBIDDEN_SPLIT_RE = re.compile(r"[、,／/・]+")
_SKIP_FORBIDDEN_TOKENS = frozenset({"", "...", "…", "-", "—", "なし", "特になし"})

_CASUAL_SSU_RE = re.compile(r"っす(?![ぐうる])")
_CASUAL_LAUGH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9./])[wｗ]{2,}(?![A-Za-z0-9./])|(?<![A-Za-z0-9])[wｗ](?=$|[\s。、！？!?\n]))"
)
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _clean_forbidden_token(raw: str) -> str:
    token = raw.strip(" \t\r\n\u3000")
    if not token or _PLACEHOLDER_PREFIX_RE.match(token):
        return ""
    if token.startswith(("（例", "(例", "（記入例", "(記入例", "（未記入", "(未記入")):
        return ""
    while len(token) >= 2 and (
        (token[0] == "「" and token[-1] == "」")
        or (token[0] == "『" and token[-1] == "』")
        or (token[0] == '"' and token[-1] == '"')
        or (token[0] == "'" and token[-1] == "'")
        or (token[0] == "“" and token[-1] == "”")
    ):
        token = token[1:-1].strip(" \t\r\n\u3000")
    token = token.strip(" \t\r\n\u3000\"'「」『』“”〜～")
    if not token or _PLACEHOLDER_PREFIX_RE.match(token) or token in _SKIP_FORBIDDEN_TOKENS:
        return ""
    return token


def parse_profile(path: str, scene: str | None = None) -> list[str]:
    """profile.md から「絶対に言わない表現」の一覧を抽出する。"""
    text = _normalize_newlines(_read_utf8(os.path.expanduser(path)))
    common_items: list[str] = []
    scoped_items: dict[str, list[str]] = {}
    current_section: str | None = None

    for line in text.split("\n"):
        heading = _PROFILE_HEADING_RE.match(line)
        if heading:
            scene_m = _PROFILE_SCENE_RE.search(heading.group(1))
            current_section = scene_m.group(1).lower() if scene_m else None
            continue

        m = _FORBIDDEN_LINE_RE.match(line)
        if not m:
            continue

        rhs = _PLACEHOLDER_PAREN_RE.sub("", m.group(1)).strip()
        if not rhs:
            continue

        parsed: list[str] = []
        for raw_token in _FORBIDDEN_SPLIT_RE.split(rhs):
            cleaned = _clean_forbidden_token(raw_token)
            if cleaned:
                parsed.append(cleaned)

        if not parsed:
            continue

        if current_section is None or current_section == "common":
            common_items.extend(parsed)
        else:
            scoped_items.setdefault(current_section, []).extend(parsed)

    combined: list[str] = list(common_items)
    if scene is not None:
        combined.extend(scoped_items.get(scene.lower(), []))
    elif not common_items:
        for items in scoped_items.values():
            combined.extend(items)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in combined:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def check_profile_drift(
    orig: str,
    draft: str,
    forbidden: list[str],
    scene: str | None = None,
) -> list[str]:
    """原文から直した文への変更で、個人ルール違反や formal 場面の崩し表現が増えていないか調べる。"""
    findings: list[str] = []
    orig_norm = unicodedata.normalize("NFKC", _normalize_newlines(orig))
    draft_norm = unicodedata.normalize("NFKC", _normalize_newlines(draft))

    seen_forbidden: set[str] = set()
    for w in forbidden:
        if not w or w in seen_forbidden:
            continue
        seen_forbidden.add(w)
        w_norm = unicodedata.normalize("NFKC", w)
        if not w_norm:
            continue
        if draft_norm.count(w_norm) > orig_norm.count(w_norm):
            findings.append(f"[個人ルール] 避ける表現を検出: 「{w}」")

    if scene == "formal":
        orig_text = _normalize_newlines(orig)
        draft_text = _normalize_newlines(draft)
        casual_checks = (
            (_CASUAL_SSU_RE, "「っす」"),
            (_CASUAL_LAUGH_RE, "「w」"),
            (_EMOJI_RE, "絵文字"),
        )
        for pattern, label in casual_checks:
            if len(pattern.findall(draft_text)) > len(pattern.findall(orig_text)):
                findings.append(f"[場面ガード] formal 場面で崩し表現を検出: {label}")

    return findings


def _format_finding(item: str) -> str:
    """check_meaning.py の内部メッセージを CLI 表示向けに整える（既存ロジックは無変更）。"""
    replacements = (
        ("[確かさ・強さ]", "[確度・必須度]"),
        ("原文にあって直した文で減ったもの:", "削除（原文のみ）:"),
        ("直した文で増えたもの:", "追加（編集後のみ）:"),
        ("の言い方が減った", "表現が減少"),
        ("の言い方が増えた", "表現が増加"),
        ("」が減った", "」が減少"),
        ("」が増えた", "」が増加"),
        ("否定の表現が減った", "否定表現が減少"),
        ("否定の表現が増えた", "否定表現が増加"),
    )
    out = item
    for old, new in replacements:
        out = out.replace(old, new)
    return out


def _parse_extensions(ext_arg: str) -> tuple[str, ...]:
    exts = []
    for raw in ext_arg.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        if item not in exts:
            exts.append(item)
    return tuple(exts)


def _matches_ext(path: str, exts: tuple[str, ...]) -> bool:
    if not exts:
        return True
    lower = path.lower()
    return any(lower.endswith(ext) for ext in exts)


def _read_hook_file_path() -> str | None:
    """--hook 時に標準入力の JSON から対象ファイルパスを安全に取り出す。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        if hasattr(sys.stdin, "buffer"):
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        else:
            raw = sys.stdin.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "TargetFile", "Path"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    for key in ("file_path", "path", "TargetFile", "Path"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _find_git_root(probe_dir: str) -> str | None:
    try:
        proc = _run_git(["rev-parse", "--show-toplevel"], cwd=probe_dir)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.decode("utf-8", errors="replace").strip()
    return os.path.realpath(root) if root else None


def _to_repo_relpath(path: str, repo_root: str) -> str | None:
    abs_path = os.path.realpath(os.path.abspath(path))
    abs_root = os.path.realpath(os.path.abspath(repo_root))
    try:
        common = os.path.commonpath([abs_root, abs_path])
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(abs_root):
        return None
    rel = os.path.relpath(abs_path, abs_root)
    if rel == ".":
        return "."
    return rel.replace(os.sep, "/")


def _git_changed_files(
    repo_root: str,
    base: str,
    staged: bool,
    pathspecs: list[str] | None = None,
) -> list[str]:
    cmd = ["-c", "core.quotepath=false", "diff", "-z", "--name-only", "--diff-filter=M"]
    if staged:
        cmd.append("--cached")
    cmd.append(base)
    if pathspecs:
        cmd.append("--")
        cmd.extend(pathspecs)
    proc = _run_git(cmd, cwd=repo_root)
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for chunk in proc.stdout.split(b"\x00"):
        if not chunk:
            continue
        try:
            rel = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        out.append(rel.replace("\\", "/"))
    return out


def _git_show_utf8(repo_root: str, spec: str) -> str | None:
    proc = _run_git(["show", spec], cwd=repo_root)
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_working_tree_utf8(repo_root: str, posix_rel: str) -> str | None:
    full_path = os.path.join(repo_root, *posix_rel.split("/"))
    try:
        with open(full_path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def cmd_diff(args: argparse.Namespace) -> int:
    try:
        src = _read_utf8(args.orig)
        dst = _read_utf8(args.draft)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ファイルを読み込めませんでした: {exc}", file=sys.stderr)
        return 2

    forbidden: list[str] = []
    if args.profile:
        try:
            forbidden = parse_profile(args.profile, scene=args.scene)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"プロフィールファイルを読み込めませんでした: {exc}", file=sys.stderr)
            return 2

    request = _load_request(args.request)
    items = revert_broken.remaining(src, dst, request)
    if forbidden or args.scene:
        items = items + check_profile_drift(src, dst, forbidden, scene=args.scene)
    items = [_format_finding(x) for x in items]

    if args.json:
        payload = {
            "orig": args.orig,
            "draft": args.draft,
            "clean": len(items) == 0,
            "count": len(items),
            "findings": items,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if not items:
            print("見直す候補はありません。")
        else:
            print(f"見直す候補が{len(items)}件あります。直した文の該当箇所を原文と読み比べてください。")
            for item in items:
                print("- " + item)

    return 1 if items else 0


def cmd_fix(args: argparse.Namespace) -> int:
    try:
        src = _read_utf8(args.orig)
        dst = _read_utf8(args.draft)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"ファイルを読み込めませんでした: {exc}", file=sys.stderr)
        return 2

    request = _load_request(args.request)
    logs: list[str] = []
    rest = revert_broken.remaining(src, dst, request)
    n = len(rest)
    for _ in range(6):
        new, log = revert_broken.one_pass(src, dst, request, rest)
        next_rest = revert_broken.remaining(src, new, request)
        m = len(next_rest)
        if not log or m >= n:
            break
        dst, n = new, m
        rest = next_rest
        logs += log

    comma_logs: list[str] = []
    if not args.no_trim_commas:
        dst, comma_logs = trim_commas.trim(dst)

    try:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(dst)
    except OSError as exc:
        print(f"出力ファイルを書き込めませんでした: {exc}", file=sys.stderr)
        return 2

    if logs:
        print(f"{len(logs)}か所を原文に戻しました。")
        for entry in logs:
            print("- " + entry)
    else:
        print("戻した所はありません。")

    if not args.no_trim_commas:
        if comma_logs:
            print(f"{len(comma_logs)}か所の読点を外しました。")
            for entry in comma_logs:
                print("- " + entry)
        else:
            print("外した読点はありません。")

    if rest:
        print(f"残っている候補が{len(rest)}件あります（読み比べて、同じ意味ならそのままでよい）：")
        for item in rest:
            print("- " + _format_finding(item))

    if args.fail_on_remaining and rest:
        return 1
    return 0


def cmd_git(args: argparse.Namespace) -> int:
    exts = _parse_extensions(args.ext)
    request = _load_request(args.request)
    hook_path: str | None = None

    if args.hook:
        hook_path = _read_hook_file_path()
        if hook_path is not None:
            if not _matches_ext(hook_path, exts):
                if args.json:
                    print(json.dumps({
                        "base": args.base,
                        "staged": args.staged,
                        "clean": True,
                        "checked": 0,
                        "flagged": 0,
                        "files": [],
                    }, ensure_ascii=False, indent=2))
                return 0

    forbidden: list[str] = []
    if args.profile:
        try:
            forbidden = parse_profile(args.profile, scene=args.scene)
        except (OSError, UnicodeDecodeError) as exc:
            if not args.hook:
                print(f"プロフィールファイルを読み込めませんでした: {exc}", file=sys.stderr)
                return 2
            forbidden = []

    probe_dir = os.getcwd()
    if hook_path is not None:
        candidate_dir = os.path.dirname(os.path.abspath(hook_path))
        if os.path.isdir(candidate_dir):
            probe_dir = candidate_dir
    elif args.paths:
        first_abs = os.path.abspath(args.paths[0])
        candidate_dir = first_abs if os.path.isdir(first_abs) else os.path.dirname(first_abs)
        if os.path.isdir(candidate_dir):
            probe_dir = candidate_dir

    repo_root = _find_git_root(probe_dir)
    if repo_root is None:
        if args.hook:
            return 0
        print("git リポジトリの中で実行してください。", file=sys.stderr)
        return 2

    verify = _run_git(["rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}"], cwd=repo_root)
    if verify.returncode != 0:
        if args.hook:
            return 0
        print(f"比較元のコミットが見つかりません: {args.base}", file=sys.stderr)
        return 2

    targets: list[str] = []
    seen: set[str] = set()

    def _add_target(rel: str) -> None:
        if rel not in seen and _matches_ext(rel, exts):
            seen.add(rel)
            targets.append(rel)

    explicit_paths = [hook_path] if hook_path is not None else list(args.paths)
    if explicit_paths:
        dir_specs: list[str] = []
        for raw_path in explicit_paths:
            rel = _to_repo_relpath(raw_path, repo_root)
            if rel is None:
                continue
            abs_p = os.path.abspath(raw_path)
            if rel == "." or os.path.isdir(abs_p):
                if rel != ".":
                    dir_specs.append(rel)
                else:
                    for changed in _git_changed_files(repo_root, args.base, args.staged):
                        _add_target(changed)
            else:
                _add_target(rel)
        if dir_specs:
            for changed in _git_changed_files(repo_root, args.base, args.staged, dir_specs):
                _add_target(changed)
    else:
        for changed in _git_changed_files(repo_root, args.base, args.staged):
            _add_target(changed)

    checked = 0
    flagged_files: list[dict[str, object]] = []

    for rel in targets:
        orig_raw = _git_show_utf8(repo_root, f"{args.base}:{rel}")
        if orig_raw is None:
            continue
        if args.staged:
            curr_raw = _git_show_utf8(repo_root, f":{rel}")
        else:
            curr_raw = _read_working_tree_utf8(repo_root, rel)
        if curr_raw is None:
            continue

        checked += 1
        orig = _normalize_newlines(orig_raw)
        curr = _normalize_newlines(curr_raw)
        if orig == curr:
            continue

        findings = revert_broken.remaining(orig, curr, request)
        if forbidden or args.scene:
            findings = findings + check_profile_drift(orig, curr, forbidden, scene=args.scene)
        findings = [_format_finding(x) for x in findings]
        if findings:
            flagged_files.append({
                "path": rel,
                "count": len(findings),
                "findings": findings,
            })

    total_findings = sum(int(entry["count"]) for entry in flagged_files)

    if args.json:
        payload = {
            "base": args.base,
            "staged": args.staged,
            "clean": len(flagged_files) == 0,
            "checked": checked,
            "flagged": len(flagged_files),
            "files": flagged_files,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.hook:
        if flagged_files:
            lines = [f"nihongo-check: 意味が変わった可能性がある箇所を検出しました（base: {args.base}）"]
            for entry in flagged_files:
                lines.append(f"[{entry['path']}] ({entry['count']}件)")
                for item in entry["findings"]:  # type: ignore[union-attr]
                    lines.append(f"- {item}")
            lines.append("原文と読み比べ、意図しない変更があれば元に戻してください。")
            print("\n".join(lines), file=sys.stderr)
            return 2
        return 0

    if not args.json:
        if flagged_files:
            print(f"意味が変わったかもしれない箇所が {len(flagged_files)} ファイル（{total_findings}件）見つかりました（base: {args.base}）。")
            for entry in flagged_files:
                print(f"\n[{entry['path']}] {entry['count']}件")
                for item in entry["findings"]:  # type: ignore[union-attr]
                    print("- " + item)
        elif checked == 0:
            print("チェック対象の変更ファイルはありません。")
        else:
            print(f"{checked}件のファイルを確認しました。見直す候補はありません。")

    return 1 if flagged_files else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nihongo-check",
        description="原文と直した文の意味のズレ検出・自動復旧・読点整理を行う日本語チェック CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_diff = sub.add_parser("diff", help="原文と直した文を比べて意味のズレ候補を出す")
    p_diff.add_argument("orig", help="原文ファイルのパス")
    p_diff.add_argument("draft", help="直した文ファイルのパス")
    p_diff.add_argument("--request", default="", help="依頼文（ファイルパスまたは文字列）")
    p_diff.add_argument("--profile", default=None, help="書き方メモ（profile.md）のパス")
    p_diff.add_argument(
        "--scene",
        choices=("casual", "work", "formal"),
        default=None,
        help="文章の場面（casual / work / formal）",
    )
    p_diff.add_argument("--json", action="store_true", help="JSON 形式で出力する")
    p_diff.set_defaults(func=cmd_diff)

    p_fix = sub.add_parser("fix", help="意味が変わった文を原文に戻し、要らない読点を外す")
    p_fix.add_argument("orig", help="原文ファイルのパス")
    p_fix.add_argument("draft", help="直した文ファイルのパス")
    p_fix.add_argument("out", help="出力ファイルのパス")
    p_fix.add_argument("--request", default="", help="依頼文（ファイルパスまたは文字列）")
    p_fix.add_argument("--no-trim-commas", action="store_true", help="読点の自動整理を行わない")
    p_fix.add_argument("--fail-on-remaining", action="store_true", help="候補が残った場合に終了コード 1 で終わる")
    p_fix.set_defaults(func=cmd_fix)

    p_git = sub.add_parser("git", help="git で変更したテキスト・Markdown の意味のズレを確認する")
    p_git.add_argument("--base", default="HEAD", help="比較元の git ref（既定: HEAD）")
    p_git.add_argument("--staged", action="store_true", help="ステージ済み（index）の変更を対象にする")
    p_git.add_argument("--ext", default=".md,.txt", help="対象とする拡張子（カンマ区切り、既定: .md,.txt）")
    p_git.add_argument("--request", default="", help="依頼文（ファイルパスまたは文字列）")
    p_git.add_argument("--profile", default=None, help="書き方メモ（profile.md）のパス")
    p_git.add_argument(
        "--scene",
        choices=("casual", "work", "formal"),
        default=None,
        help="文章の場面（casual / work / formal）",
    )
    p_git.add_argument("--hook", action="store_true", help="PostToolUse フック向けモード（候補検出時は stderr 出力＋終了コード 2）")
    p_git.add_argument("--json", action="store_true", help="JSON 形式で出力する")
    p_git.add_argument("paths", nargs="*", help="対象を絞るファイルまたはディレクトリ")
    p_git.set_defaults(func=cmd_git)

    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
