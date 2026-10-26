#!/usr/bin/env python3
"""Read Claude Desktop Code session indices; never reads credentials or transcripts."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def date(value):
    if value is None:
        return "unknown"
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc).isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"


def collect(root, labels):
    rows, warnings = [], []
    if not root.is_dir():
        raise FileNotFoundError(f"Desktop Code index directory not found: {root}")
    for path in sorted(root.glob("*/*/local_*.json")):
        if path.is_symlink() or any(p.is_symlink() for p in [path.parent, path.parent.parent]):
            warnings.append(f"Skipped symlink: {path.name}")
            continue
        try:
            d = load(path)
            if not isinstance(d, dict):
                raise ValueError("index is not an object")
            account = path.parent.parent.name
            rows.append(dict(account=labels.get(account, account), account_uuid=account,
                             organization=path.parent.name, id=d.get("cliSessionId"),
                             title=d.get("title") or "(без названия)",
                             last_activity=date(d.get("lastActivityAt")),
                             archived=bool(d.get("isArchived", False))))
        except (ValueError, OSError) as exc:
            warnings.append(f"Unreadable index {path.name}: {type(exc).__name__}")
    return sorted(rows, key=lambda r: r["last_activity"], reverse=True), warnings


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path.home()/"Library/Application Support/Claude/claude-code-sessions")
    p.add_argument("--account", default="", help="Filter by account label or UUID")
    p.add_argument("--query", default="", help="Filter by title or ID")
    p.add_argument("--json", action="store_true", help="Output at most five preview rows as JSON")
    p.add_argument("--output", type=Path, default=Path.home()/"Documents/Claude Sessions/sessions.txt")
    p.add_argument("--labels", type=Path, default=Path.home()/".config/claude-sessions/accounts.json")
    args = p.parse_args()
    try:
        labels = load(args.labels) if args.labels.exists() else {}
        if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k,v in labels.items()):
            raise ValueError("Account labels must be a UUID-to-label JSON object")
        rows, warnings = collect(args.root, labels)
    except (ValueError, OSError) as exc:
        p.exit(1, f"sessions: {exc}\n")
    all_rows = rows
    rows = [r for r in rows if args.account.lower() in (r["account"]+" "+r["account_uuid"]).lower()
            and args.query.lower() in (r["title"]+" "+str(r["id"])).lower()]
    header = "Аккаунт | ID для resume | Название | Последняя активность (с часовым поясом)"
    def line(r):
        clean = lambda value: str(value).replace("\n", " ").replace("\r", " ").replace("|", "/")
        return " | ".join(map(clean, [r["account"], r["id"] or "нет CLI ID", r["title"]+(" [архив]" if r["archived"] else ""), r["last_activity"]]))
    content = [header] + [line(r) for r in all_rows]
    content += [f"\nВсего записей: {len(all_rows)}. Один ID может встречаться в нескольких аккаунтах."]
    content += ["WARNING: " + w for w in warnings]
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        import os
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as f:
            temp_path = Path(f.name)
            f.write("\n".join(content) + "\n")
        os.replace(temp_path, args.output)
    except OSError as exc:
        p.exit(1, f"sessions: TXT export failed: {exc}\n")
    if args.json:
        print(json.dumps(dict(file=str(args.output.resolve()), total=len(all_rows), matched=len(rows), sessions=rows[:5], warnings=warnings), ensure_ascii=False, indent=2))
    else:
        print(f"Полный список ({len(all_rows)} записей): {args.output.resolve()}")
        print(header)
        for r in rows[:5]:
            print(line(r))
        for w in warnings:
            print("WARNING:", w)



if __name__ == "__main__":
    main()
