#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KernelSU Auto Sync v4.2.3: upstream/master + dev/personal -> origin/main.

KHÔNG dùng sync/state hay sync-state.json.
- Mỗi lần chạy, lấy upstream và danh sách commit cá nhân từ hai nhánh nguồn.
- Rebuild main bằng cherry-pick; tạo tag fork từ ĐÚNG commit upstream gắn tag,
  kể cả khi commit tag nằm ngoài upstream/master hoặc trên nhánh orphan.
- Chỉ push khi rebuild và kiểm tra hoàn toàn thành công.
- Giữ nguyên author/date/message của commit cá nhân; commit SHA thay đổi.
- --push mới thực sự cập nhật remote; mặc định chỉ kiểm tra (dry-run).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class SyncError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, errors="replace", env=env)
    if p.returncode and check:
        raise SyncError(f"git {' '.join(args)} thất bại:\n{p.stderr}\n{p.stdout}".strip())
    return p.stdout.strip() if p.returncode == 0 else ""


def sha(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


def remote_sha(repo: Path, remote: str, kind: str, name: str) -> str | None:
    for row in git(repo, "ls-remote", "--refs", remote, f"refs/{kind}/{name}").splitlines():
        oid, ref = row.split("\t", 1)
        if ref == f"refs/{kind}/{name}":
            return oid
    return None


def is_ancestor(repo: Path, old: str, new: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", old, new],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def personal_commits(repo: Path, base: str, tip: str) -> list[str]:
    # personal/base chỉ cần là tổ tiên của dev/personal; không cần nằm trên upstream mới.
    if not is_ancestor(repo, base, tip):
        raise SyncError("personal/base không thuộc lịch sử dev/personal")
    commits = git(repo, "rev-list", "--reverse", f"{base}..{tip}").splitlines()
    parent = base
    for oid in commits:
        lineage = git(repo, "rev-list", "--parents", "-n", "1", oid).split()
        if len(lineage) != 2 or lineage[1] != parent:
            raise SyncError("dev/personal phải là chuỗi commit tuyến tính, không chứa merge")
        parent = oid
    return commits


def author_identity(repo: Path, oid: str) -> str:
    return git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI%x00%B", oid)


def rebuild(repo: Path, root: Path, start: str, commits: list[str], policy: str,
            check_cmd: str | None, label: str, committer_name: str,
            committer_email: str) -> tuple[str, list[dict]]:
    # Worktree tạm: không checkout, reset hay sửa working tree hiện tại của người dùng.
    worktree = root / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), start)
    applied = []
    try:
        for i, original in enumerate(commits, 1):
            env = os.environ.copy()
            env["GIT_COMMITTER_DATE"] = git(repo, "show", "-s", "--format=%cI", original)
            # Chỉ ghi ozsx là người tạo commit mới sau cherry-pick.
            # Author gốc của mỗi patch vẫn được Git giữ nguyên.
            env["GIT_COMMITTER_NAME"] = committer_name
            env["GIT_COMMITTER_EMAIL"] = committer_email
            command = ["git", "-C", str(worktree), "-c", "rerere.enabled=true",
                       "-c", "rerere.autoupdate=true", "cherry-pick",
                       "--allow-empty", "--keep-redundant-commits"]
            if policy != "fail":
                # 'theirs' ở đây là commit cá nhân đang được cherry-pick.
                command.extend(["-X", policy])
            result = subprocess.run([*command, original], capture_output=True, text=True, env=env)
            if result.returncode:
                # rerere có thể giải hết conflict, nhưng Git vẫn cần cherry-pick --continue.
                pending = git(worktree, "ls-files", "-u")
                if not pending and git(worktree, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False):
                    result = subprocess.run(["git", "-C", str(worktree), "cherry-pick", "--continue"],
                                            capture_output=True, text=True, env=env)
                if result.returncode:
                    files = git(worktree, "diff", "--name-only", "--diff-filter=U")
                    raise SyncError(f"Conflict ở {label} ({i}/{len(commits)}), file: {files or '-'}\n"
                                    f"{result.stderr}\n{result.stdout}")
            new_sha = sha(worktree, "HEAD")
            if author_identity(repo, original) != author_identity(worktree, new_sha):
                raise SyncError(f"Commit {original} bị thay đổi author/date/message")
            applied.append({"from": original, "to": new_sha,
                            "message": git(repo, "log", "-1", "--format=%s", original)})
        whitespace = subprocess.run(["git", "-C", str(worktree), "diff", "--check", start, "HEAD"],
                                    capture_output=True, text=True)
        if whitespace.returncode:
            raise SyncError(f"git diff --check thất bại ({label}): {whitespace.stdout}{whitespace.stderr}")
        if check_cmd:
            test = subprocess.run(check_cmd, shell=True, cwd=worktree, capture_output=True, text=True)
            if test.returncode:
                raise SyncError(f"Build/test thất bại ({label}): {check_cmd}\n"
                                f"{test.stdout[-4000:]}\n{test.stderr[-4000:]}")
        return sha(worktree, "HEAD"), applied
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))


def upstream_tags_missing(repo: Path, suffix: str, limit: int) -> list[tuple[str, str, str]]:
    # Không có state: upstream có tag X, origin CHƯA có X-personal => tạo.
    # Tag có thể trỏ đến commit KHÔNG nằm trên upstream/master, ví dụ nhánh release
    # riêng, tag cổ sau force-push hoặc thậm chí commit orphan chỉ được giữ bởi tag.
    # Mỗi tag -personal phải là: <commit upstream được tag> + ĐÚNG n commit cá nhân.
    # Không dùng is_ancestor(tag, upstream/master): điều đó làm mất tag release riêng.
    fork_tags = {row.split("\t", 1)[1].removeprefix("refs/tags/")
                 for row in git(repo, "ls-remote", "--refs", "--tags", "origin").splitlines()}
    candidates = []
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/sync/upstream-tags/").splitlines():
        name = ref.removeprefix("refs/sync/upstream-tags/")
        dest = name + suffix
        if dest in fork_tags:
            continue  # Tag đã phát hành không được viết lại.
        verify = subprocess.run(["git", "-C", str(repo), "check-ref-format", f"refs/tags/{dest}"],
                                capture_output=True)
        if verify.returncode:
            raise SyncError(f"Tên tag fork không hợp lệ: {dest}")
        if git(repo, "show-ref", "--verify", f"refs/tags/{dest}", check=False):
            raise SyncError(f"Tag {dest} đã có ở local nhưng chưa có ở origin")
        # Bóc annotated/lightweight tag về commit thật.
        # Nếu upstream dùng tag trỏ vào blob/tree thì dừng rõ ràng, không tạo tag sai.
        try:
            tagged_commit = sha(repo, ref)
        except SyncError as exc:
            raise SyncError(f"Tag upstream {name} không trỏ tới commit: {exc}") from exc
        candidates.append((name, tagged_commit, dest))
    candidates.sort(key=lambda x: x[0])
    if limit and len(candidates) > limit:
        raise SyncError(f"Có {len(candidates)} tag chưa tạo, vượt --max-new-tags={limit}")
    return candidates


def report(repo: Path, data: dict, destination: str) -> None:
    folder = Path(destination)
    if not folder.is_absolute():
        folder = repo / folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ["# KernelSU Auto Sync (không sync-state)", "",
             f"- Trạng thái: **{data['status']}**",
             f"- Upstream: `{data.get('upstream', '-')}`",
             f"- Main cũ: `{data.get('main_before', '-')}`",
             f"- Main mới: `{data.get('main_after', '-')}`",
             f"- Số commit cá nhân: {len(data.get('personal_commits', []))}",
             f"- Tag tạo mới: {len(data.get('tags_new', []))}",
             f"- Conflict policy: `{data.get('strategy')}`",
             f"- Backup: `{data.get('backup', '-')}`", ""]
    for entry in data.get("applied", []):
        lines.append(f"- `{entry['from'][:12]}` → `{entry['to'][:12]}`: {entry['message']}")
    for entry in data.get("tags_new", []):
        lines.append(f"- Tag `{entry['upstream_tag']}` (gốc `{entry['upstream_commit'][:12]}`) → "
                     f"`{entry['fork_tag']}` (`{entry['target'][:12]}`, +{entry['personal_count']} commit)")
    if data.get("error"):
        lines.extend(["", "## Lỗi", "", "```text", data["error"], "```"])
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {folder / 'report.md'}")


def sync(repo: Path, args: argparse.Namespace, data: dict) -> None:
    git(repo, "remote", "get-url", "origin")
    if not git(repo, "remote", "get-url", "upstream", check=False):
        git(repo, "remote", "add", "upstream", args.upstream_url)
    git(repo, "fetch", "--no-tags", "upstream",
        f"+refs/heads/{args.upstream_branch}:refs/remotes/upstream/{args.upstream_branch}")
    git(repo, "fetch", "--no-tags", "origin",
        *[f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
          for branch in (args.target, args.personal, args.base)])
    git(repo, "fetch", "--no-tags", "--prune", "upstream", "+refs/tags/*:refs/sync/upstream-tags/*")
    upstream = sha(repo, f"refs/remotes/upstream/{args.upstream_branch}")
    old_main = sha(repo, f"refs/remotes/origin/{args.target}")
    base = sha(repo, f"refs/remotes/origin/{args.base}")
    personal_tip = sha(repo, f"refs/remotes/origin/{args.personal}")
    commits = personal_commits(repo, base, personal_tip)
    data.update(upstream=upstream, main_before=old_main, personal_commits=commits)
    # Không so sánh với sync/state. main luôn được DỰNG LẠI từ upstream + personal.
    candidates = upstream_tags_missing(repo, args.tag_suffix, args.max_new_tags)
    staging_before = remote_sha(repo, "origin", "heads", args.staging)
    with tempfile.TemporaryDirectory(prefix="kernelsu-sync-") as tmp:
        workdir = Path(tmp)
        main_sha, applied = rebuild(repo, workdir, upstream, commits, args.strategy, args.check_cmd,
                                    "main", args.tagger_name, args.tagger_email)
        data.update(main_after=main_sha, applied=applied)
        for name, target, tag_name in candidates:
            release_sha, tag_applied = rebuild(repo, workdir, target, commits, args.strategy,
                                               args.check_cmd, f"tag:{name}", args.tagger_name,
                                               args.tagger_email)
            # Bất biến quan trọng: bỏ đúng n parent đầu tiên phải tới tag upstream.
            # --keep-redundant-commits bảo toàn cả commit rỗng sau cherry-pick.
            actual_parent = sha(repo, f"{release_sha}~{len(commits)}")
            if len(tag_applied) != len(commits) or actual_parent != target:
                raise SyncError(f"Tag {name}: không đảm bảo có đúng {len(commits)} commit cá nhân")
            # Annotated tag lưu danh tính tagger riêng biệt với author/committer
            # của các commit P1..Pn. Phải sử dụng email thuộc tài khoản GitHub
            # muốn được hiển thị trong phần thông tin tag (ví dụ ozsx).
            # Gán trực tiếp thay vì setdefault để tránh biến môi trường của runner
            # vô tình ghi đè danh tính đã chọn qua --tagger-name/email.
            env = os.environ.copy()
            env["GIT_COMMITTER_NAME"] = args.tagger_name
            env["GIT_COMMITTER_EMAIL"] = args.tagger_email
            git(repo, "tag", "-a", tag_name, release_sha, "-m",
                f"Personal build from {name}\nUpstream: {target}\nPersonal: {personal_tip}", env=env)
            data["tags_new"].append({"upstream_tag": name, "fork_tag": tag_name,
                                     "upstream_commit": target, "target": release_sha,
                                     "personal_count": len(commits)})

    if main_sha == old_main and staging_before == main_sha and not candidates:
        data["status"] = "skip"
        print("SKIP: main và tags không thay đổi")
        return
    if not args.push:
        data["status"] = "dry-run"
        print("DRY-RUN OK: dùng --push để cập nhật main, staging và tags")
        return

    # Kiểm tra nguồn có đổi trong lúc build; lease kiểm tra main + staging lúc push.
    if remote_sha(repo, "upstream", "heads", args.upstream_branch) != upstream:
        raise SyncError("Upstream thay đổi trong lúc xử lý, hãy chạy lại")
    if remote_sha(repo, "origin", "heads", args.personal) != personal_tip:
        raise SyncError("dev/personal thay đổi trong lúc xử lý, hãy chạy lại")
    if remote_sha(repo, "origin", "heads", args.base) != base:
        raise SyncError("personal/base thay đổi trong lúc xử lý, hãy chạy lại")
    for t in data["tags_new"]:
        # Tag upstream có thể bị di chuyển trong lúc build. Đừng phát hành bản
        # -personal từ target cũ nếu tag gốc vừa đổi nơi trỏ tới.
        fetched_tag_object = git(repo, "rev-parse", f"refs/sync/upstream-tags/{t['upstream_tag']}")
        current_tag_object = remote_sha(repo, "upstream", "tags", t["upstream_tag"])
        if current_tag_object != fetched_tag_object:
            raise SyncError(f"Tag upstream {t['upstream_tag']} đã đổi trong lúc build, hãy chạy lại")
        if remote_sha(repo, "origin", "tags", t["fork_tag"]):
            raise SyncError(f"Tag {t['fork_tag']} vừa được người khác tạo, không ghi đè")

    # Lưu bản main cũ trước khi force-push để có thể quay lại.
    refspecs = [f"+{main_sha}:refs/heads/{args.target}",
                f"+{main_sha}:refs/heads/{args.staging}"]
    leases = [f"--force-with-lease=refs/heads/{args.target}:{old_main}",
              f"--force-with-lease=refs/heads/{args.staging}:{staging_before or ''}"]
    if main_sha != old_main:
        backup = f"sync/backups/{old_main}"
        backup_sha = remote_sha(repo, "origin", "heads", backup)
        if backup_sha and backup_sha != old_main:
            raise SyncError(f"Backup {backup} đã tồn tại nhưng SHA khác main cũ")
        if not backup_sha:
            refspecs.append(f"{old_main}:refs/heads/{backup}")
        data["backup"] = backup
    refspecs.extend(f"refs/tags/{t['fork_tag']}:refs/tags/{t['fork_tag']}" for t in data["tags_new"])
    # atomic: nếu push tags/backup/staging thất bại, main cũng không bị đổi.
    # force-with-lease: không ghi đè bản main mới hơn phát sinh giữa lúc đang chạy.
    git(repo, "push", "--atomic", *leases, "origin", *refspecs)
    data["status"] = "pushed"
    print(f"PUSH OK: main={main_sha}, tag mới={len(data['tags_new'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="KernelSU auto sync, không dùng sync-state")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--upstream-url", default="https://github.com/backslashxx/KernelSU.git")
    parser.add_argument("--upstream-branch", default="master")
    parser.add_argument("--personal", default="dev")
    parser.add_argument("--base", default="base")
    parser.add_argument("--target", default="main")
    parser.add_argument("--staging", default="sync/staging")
    parser.add_argument("--strategy", choices=("theirs", "ours", "fail"), default="theirs")
    parser.add_argument("--tag-suffix", default="-personal")
    # Dùng ozsx làm tagger và committer; yêu cầu email xác thực để không giả mạo.
    # Author gốc của các patch cá nhân KHÔNG thay đổi.
    parser.add_argument("--tagger-name", default="ozsx")
    parser.add_argument("--tagger-email", default=os.environ.get("SYNC_TAGGER_EMAIL", ""))
    parser.add_argument("--max-new-tags", type=int, default=0, help="0 = không giới hạn")
    parser.add_argument("--check-cmd", default=None)
    parser.add_argument("--report-dir", default="sync-reports")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    data = {"status": "running", "strategy": args.strategy, "tags_new": [],
            "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        if git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise SyncError(f"Không phải Git worktree: {repo}")
        if git(repo, "rev-parse", "--is-shallow-repository") == "true":
            raise SyncError("Phải dùng full history (fetch-depth: 0)")
        branches = (args.personal, args.base, args.target, args.staging)
        if len(set(branches)) != 4:
            raise SyncError("personal/base/target/staging phải là các nhánh khác nhau")
        if not args.tag_suffix or args.max_new_tags < 0:
            raise SyncError("tag-suffix không được rỗng; max-new-tags phải >= 0")
        # Không đưa ký tự xuống dòng vào metadata tag để tránh tagger không hợp lệ.
        if (not args.tagger_name.strip() or not args.tagger_email.strip()
                or "@" not in args.tagger_email
                or any(ch in args.tagger_name + args.tagger_email for ch in "\r\n")):
            raise SyncError("Thiếu hoặc sai email GitHub của ozsx: đặt SYNC_TAGGER_EMAIL hoặc --tagger-email; không dùng email bot giả định")
        for name in (*branches, args.upstream_branch):
            if subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                raise SyncError(f"Tên branch không hợp lệ: {name}")
        sync(repo, args, data)
        return 0
    except (SyncError, OSError) as exc:
        data.update(status="failed", error=str(exc))
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2
    finally:
        # Dọn những tag vừa tạo ở local; không xóa tag đã có từ trước.
        for t in data["tags_new"]:
            git(repo, "tag", "-d", t["fork_tag"], check=False)
        try:
            report(repo, data, args.report_dir)
        except OSError as exc:
            print(f"Không ghi được report: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
