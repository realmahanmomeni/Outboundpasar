#!/usr/bin/env python3
"""
Stage A guarded repair: re-bind evidence-matched NULL-tag hosts to OC destination virtual inbounds.

Scope (hard-coded, independent of the manifest):
  onlineuser  workspace 10709 / tenant 7416 : panels 5283 5291 5292 5295 5296 5297 5909  (72 rows)
  moji        workspace 11036 / tenant 7832 : panels 5907 5908                           (11 rows)

Per approved row exactly three things change:
  1. INSERT one ``inbounds`` row with tag ``oc_<panel>_d_<destination_config_id>``
  2. UPDATE ``oc_panel_destination_hosts.virtual_inbound_tag`` NULL -> tag   (that row only)
  3. UPDATE ``hosts.inbound_tag``                                NULL -> tag   (that host only)

Nothing is deleted.  Group membership, users, admins, panels, other hosts and Outbound Center are
never written or contacted.  ``ensure_panel_destination_hosts_materialized`` is NOT used.

Modes
  (default)    dry run: read-only REPEATABLE READ transaction, every precondition checked, nothing written
  --apply      one panel per invocation, one transaction, strict assertions, rollback on any mismatch
  --rollback   exact inverse of --apply for one panel (aborts if any membership references the tags)

The DSN is read from the STAGEA_DSN environment variable and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

# --------------------------------------------------------------------------------------------
# Hard-coded scope.  The manifest must agree with it exactly or the script aborts.
# --------------------------------------------------------------------------------------------
PANEL_SCOPE: dict[int, dict] = {
    5283: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=19),
    5291: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=12),
    5292: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=8),
    5295: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=4),
    5296: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=12),
    5297: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=4),
    5909: dict(admin="onlineuser", workspace=10709, tenant=7416, rows=13),
    5907: dict(admin="moji", workspace=11036, tenant=7832, rows=1),
    5908: dict(admin="moji", workspace=11036, tenant=7832, rows=10),
}
# Rows/hosts that must never be touched (five informational onlineuser rows, ambiguous moji row 3219
# and its two identical hosts) plus the whole of Stage B.
FORBIDDEN_DEST_IDS = {2521, 2522, 2531, 2536, 2550, 3219}
FORBIDDEN_HOST_IDS = {5392, 5448}
EXPECTED_TOTAL = {"onlineuser": 72, "moji": 11}
MAX_BACKUP_AGE_S = 30 * 60

COUNT_SQL = {
    "inbounds": "select count(*) from inbounds",
    "hosts_total": "select count(*) from hosts",
    "hosts_null_tag": "select count(*) from hosts where inbound_tag is null",
    "hosts_no_workspace": "select count(*) from hosts where workspace_id is null",
    "hosts_bound_shadowsocks": "select count(*) from hosts where inbound_tag = 'Shadowsocks TCP'",
    "dest_total": "select count(*) from oc_panel_destination_hosts",
    "dest_null_tag": "select count(*) from oc_panel_destination_hosts where virtual_inbound_tag is null",
    "group_memberships": "select count(*) from inbounds_groups_association",
    "groups": "select count(*) from groups",
    "users": "select count(*) from users",
    "admins": "select count(*) from admins",
    "oc_panels": "select count(*) from oc_panels",
    "oc_user_mappings": "select count(*) from oc_user_mappings",
}
TAG_RE = re.compile(r"^oc_(\d+)_d_([0-9a-f]{16})$")


class Abort(Exception):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Abort(msg)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_rows(manifest_path: Path, panel_id: int) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scope = PANEL_SCOPE[panel_id]
    rows = [r for r in manifest["stageA"] if r["panel"] == panel_id]
    check(len(rows) == scope["rows"], f"panel {panel_id}: manifest has {len(rows)} rows, expected {scope['rows']}")
    stage_b_dest = {r["dest_row"] for r in manifest["stageB"]}
    stage_b_hosts = {r["host"] for r in manifest["stageB"]}
    for r in rows:
        check(r["admin"] == scope["admin"] and r["workspace"] == scope["workspace"], f"scope mismatch {r}")
        check(r["tenant"] == scope["tenant"], f"tenant mismatch {r}")
        check(r["host_tag_before"] is None, f"manifest host not NULL-tag {r}")
        check(r["dest_row"] not in FORBIDDEN_DEST_IDS | stage_b_dest, f"forbidden dest row {r['dest_row']}")
        check(r["host"] not in FORBIDDEN_HOST_IDS | stage_b_hosts, f"forbidden host {r['host']}")
        m = TAG_RE.match(r["tag"])
        check(bool(m) and int(m.group(1)) == panel_id, f"bad tag {r['tag']}")
    check(len({r["dest_row"] for r in rows}) == len(rows), "duplicate dest rows in manifest")
    check(len({r["host"] for r in rows}) == len(rows), "duplicate hosts in manifest")
    check(len({r["tag"] for r in rows}) == len(rows), "duplicate tags in manifest")
    return rows


async def scalar(conn: AsyncConnection, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar()


async def counts(conn: AsyncConnection) -> dict[str, int]:
    return {k: int(await scalar(conn, sql)) for k, sql in COUNT_SQL.items()}


async def fingerprints(conn: AsyncConnection, host_ids: list[int], dest_ids: list[int], tags: list[str]) -> dict[str, str]:
    """md5 over every row that is NOT approved (these must be identical before/after)."""
    q = {
        "hosts_other": "select coalesce(md5(string_agg(to_jsonb(h)::text, '|' order by id)),'') from hosts h "
        "where id <> all(cast(:ids as bigint[]))",
        "dest_other": "select coalesce(md5(string_agg(to_jsonb(d)::text, '|' order by id)),'') "
        "from oc_panel_destination_hosts d where id <> all(cast(:ids as bigint[]))",
        "inbounds_other": "select coalesce(md5(string_agg(to_jsonb(i)::text, '|' order by id)),'') from inbounds i "
        "where tag <> all(cast(:tags as text[]))",
        "assoc_all": "select coalesce(md5(string_agg(to_jsonb(a)::text, '|' order by inbound_id, group_id)),'') "
        "from inbounds_groups_association a",
    }
    out = {}
    out["hosts_other"] = await scalar(conn, q["hosts_other"], ids=host_ids)
    out["dest_other"] = await scalar(conn, q["dest_other"], ids=dest_ids)
    out["inbounds_other"] = await scalar(conn, q["inbounds_other"], tags=tags)
    out["assoc_all"] = await scalar(conn, q["assoc_all"])
    return out


async def row_rest_hashes(conn: AsyncConnection, host_ids: list[int], dest_ids: list[int]) -> dict:
    """Per approved row: md5 of the whole row EXCEPT the one column we are allowed to change."""
    hosts = (
        await conn.execute(
            text("select id, md5((to_jsonb(h) - 'inbound_tag')::text), inbound_tag from hosts h "
                 "where id = any(cast(:ids as bigint[]))"),
            {"ids": host_ids},
        )
    ).all()
    dest = (
        await conn.execute(
            text("select id, md5((to_jsonb(d) - 'virtual_inbound_tag')::text), virtual_inbound_tag "
                 "from oc_panel_destination_hosts d where id = any(cast(:ids as bigint[]))"),
            {"ids": dest_ids},
        )
    ).all()
    return {"hosts": {r[0]: (r[1], r[2]) for r in hosts}, "dest": {r[0]: (r[1], r[2]) for r in dest}}


async def preconditions(conn: AsyncConnection, panel_id: int, rows: list[dict]) -> list[dict]:
    scope = PANEL_SCOPE[panel_id]
    panel = (
        await conn.execute(
            text("select id, workspace_id, tenant_id, panel_type::text from oc_panels where id = :p"), {"p": panel_id}
        )
    ).first()
    check(panel is not None, f"panel {panel_id} missing")
    check(panel[1] == scope["workspace"] and panel[2] == scope["tenant"], f"panel {panel_id} scope changed: {panel[1:3]}")

    dest_ids = [r["dest_row"] for r in rows]
    host_ids = [r["host"] for r in rows]
    tags = [r["tag"] for r in rows]

    check(
        await scalar(conn, "select count(*) from oc_panel_destination_hosts where id = any(cast(:i as bigint[]))", i=dest_ids)
        == len(rows),
        "some destination rows vanished",
    )
    check(
        await scalar(conn, "select count(*) from inbounds where tag = any(cast(:t as text[]))", t=tags) == 0,
        "an approved tag already exists in inbounds",
    )
    check(
        await scalar(conn, "select count(*) from hosts where inbound_tag = any(cast(:t as text[]))", t=tags) == 0,
        "an approved tag is already used by a host",
    )
    check(
        await scalar(
            conn,
            "select count(*) from oc_panel_destination_hosts where virtual_inbound_tag = any(cast(:t as text[]))",
            t=tags,
        )
        == 0,
        "an approved tag is already used by a destination row",
    )
    check(
        await scalar(conn, "select count(*) from oc_panel_configs where virtual_inbound_tag = any(cast(:t as text[]))", t=tags)
        == 0,
        "an approved tag is already used by a catalog row",
    )

    evidence = []
    for r in rows:
        d = (
            await conn.execute(
                text(
                    "select id, panel_id, destination_config_id, display_name, virtual_inbound_tag, source_missing, "
                    "source_payload->'subscription_parsed'->>'address', source_payload->'subscription_parsed'->>'port' "
                    "from oc_panel_destination_hosts where id = :i"
                ),
                {"i": r["dest_row"]},
            )
        ).first()
        check(d[1] == panel_id, f"dest {d[0]} not in panel {panel_id}")
        check(d[4] is None, f"dest {d[0]} already has a tag")
        check(d[5] is False, f"dest {d[0]} is source_missing")
        check(r["tag"] == f"oc_{panel_id}_d_{d[2]}", f"dest {d[0]} tag mismatch")
        name, paddr, pport = (d[3] or "").strip(), d[6] or "", str(d[7] or "")
        h = (
            await conn.execute(
                text(
                    "select id, remark, address, port, inbound_tag, workspace_id, tenant_id, is_disabled "
                    "from hosts where id = :i"
                ),
                {"i": r["host"]},
            )
        ).first()
        check(h is not None, f"host {r['host']} missing")
        check(h[4] is None, f"host {h[0]} is not NULL-tag")
        check(h[5] == scope["workspace"] and h[6] == scope["tenant"], f"host {h[0]} scope {h[5]}/{h[6]} differs")
        check((h[1] or "").strip() == name, f"host {h[0]} remark differs from dest {d[0]}")
        check((h[2] or "") == paddr and str(h[3] or "") == pport, f"host {h[0]} address/port differ from dest {d[0]}")
        # exactly one host in the workspace carries this remark+address+port, and exactly one dest row claims it
        n_hosts = await scalar(
            conn,
            "select count(*) from hosts where workspace_id = :w and btrim(remark) = :r and address = :a "
            "and coalesce(port::text,'') = :p",
            w=scope["workspace"], r=name, a=paddr, p=pport,
        )
        check(n_hosts == 1, f"dest {d[0]}: {n_hosts} hosts in workspace share remark+address+port")
        n_dest = await scalar(
            conn,
            "select count(*) from oc_panel_destination_hosts d join oc_panels p on p.id = d.panel_id "
            "where p.workspace_id = :w and btrim(d.display_name) = :r "
            "and d.source_payload->'subscription_parsed'->>'address' = :a "
            "and coalesce(d.source_payload->'subscription_parsed'->>'port','') = :p",
            w=scope["workspace"], r=name, a=paddr, p=pport,
        )
        check(n_dest == 1, f"dest {d[0]}: {n_dest} destination rows claim the same remark+address+port")
        evidence.append(dict(dest=d[0], host=h[0], tag=r["tag"], remark=name[:34], addr_port=f"{paddr}:{pport}"))
    check(
        await scalar(conn, "select count(*) from hosts where id = any(cast(:i as bigint[])) and inbound_tag is null", i=host_ids)
        == len(rows),
        "host NULL-tag count mismatch",
    )
    return evidence


def expected_after(before: dict[str, int], n: int, *, inverse: bool = False) -> dict[str, int]:
    s = -1 if inverse else 1
    after = dict(before)
    after["inbounds"] += s * n
    after["hosts_null_tag"] -= s * n
    after["dest_null_tag"] -= s * n
    return after


def print_counts(label: str, before: dict, after: dict | None) -> None:
    print(f"  {label:<26}{'before':>9}{'after':>9}")
    for k in COUNT_SQL:
        a = "-" if after is None else str(after[k])
        mark = "" if after is None or after[k] == before[k] else "   <-- changes"
        print(f"  {k:<26}{before[k]:>9}{a:>9}{mark}")


async def run_panel(engine, panel_id: int, manifest_path: Path, mode: str) -> dict:
    rows = load_rows(manifest_path, panel_id)
    n = len(rows)
    dest_ids = sorted(r["dest_row"] for r in rows)
    host_ids = sorted(r["host"] for r in rows)
    tags = sorted(r["tag"] for r in rows)
    scope = PANEL_SCOPE[panel_id]
    write = mode in ("apply", "rollback")

    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text("set transaction isolation level repeatable read" + ("" if write else " read only"))
            )
            if write:
                await conn.execute(text("set local lock_timeout = '5s'"))
                await conn.execute(text("set local statement_timeout = '60s'"))
            before = await counts(conn)
            fp_before = await fingerprints(conn, host_ids, dest_ids, tags)

            print(f"\n=== panel {panel_id}  admin={scope['admin']} workspace={scope['workspace']} tenant={scope['tenant']}"
                  f"  mode={mode.upper()}  rows={n}")
            if mode != "rollback":
                evidence = await preconditions(conn, panel_id, rows)
                print(f"  preconditions OK ({n} rows: remark+address+port unique in workspace, tags absent, hosts NULL-tag)")
                for e in evidence:
                    print(f"    dest {e['dest']:>5}  host {e['host']:>5}  {e['tag']}  {e['addr_port'][:34]:<34} {e['remark']}")
            else:
                for r in rows:
                    check(await scalar(conn, "select virtual_inbound_tag from oc_panel_destination_hosts where id=:i",
                                       i=r["dest_row"]) == r["tag"], f"dest {r['dest_row']} not in applied state")
                    check(await scalar(conn, "select inbound_tag from hosts where id=:i", i=r["host"]) == r["tag"],
                          f"host {r['host']} not in applied state")
                check(await scalar(conn, "select count(*) from inbounds i join inbounds_groups_association a "
                                   "on a.inbound_id=i.id where i.tag = any(cast(:t as text[]))", t=tags) == 0,
                      "membership rows reference the tags; refusing to roll back")
                check(await scalar(conn, "select count(*) from hosts where inbound_tag = any(cast(:t as text[]))",
                                   t=tags) == n, "other hosts use the tags")
                check(await scalar(conn, "select count(*) from oc_panel_destination_hosts where virtual_inbound_tag "
                                   "= any(cast(:t as text[]))", t=tags) == n, "other dest rows use the tags")
                check(await scalar(conn, "select count(*) from oc_panel_configs where virtual_inbound_tag = "
                                   "any(cast(:t as text[]))", t=tags) == 0, "catalog rows use the tags")
                check(await scalar(conn, "select count(*) from inbounds where tag = any(cast(:t as text[]))",
                                   t=tags) == n, "inbound rows missing")
                print("  rollback preconditions OK")

            rest_before = await row_rest_hashes(conn, host_ids, dest_ids)
            planned = expected_after(before, n, inverse=(mode == "rollback"))

            if not write:
                print("  DRY RUN: nothing written. Planned operations:")
                print(f"    INSERT inbounds x{n}; UPDATE oc_panel_destination_hosts.virtual_inbound_tag x{n}; "
                      f"UPDATE hosts.inbound_tag x{n}; DELETE x0")
                print_counts("expected counts", before, planned)
                await trans.rollback()
                return dict(panel=panel_id, rows=n, before=before, after=planned, written=False)

            # ------------------------------------------------------------------ write path
            await conn.execute(text("select id from hosts where id = any(cast(:i as bigint[])) order by id for update"),
                               {"i": host_ids})
            await conn.execute(text("select id from oc_panel_destination_hosts where id = any(cast(:i as bigint[])) "
                                    "order by id for update"), {"i": dest_ids})
            if mode == "apply":
                for r in sorted(rows, key=lambda r: r["dest_row"]):
                    res = await conn.execute(text("insert into inbounds (tag) values (:t)"), {"t": r["tag"]})
                    check(res.rowcount == 1, f"insert inbound {r['tag']} rowcount {res.rowcount}")
                for r in sorted(rows, key=lambda r: r["dest_row"]):
                    res = await conn.execute(
                        text("update oc_panel_destination_hosts set virtual_inbound_tag = :t where id = :i and "
                             "panel_id = :p and destination_config_id = :c and virtual_inbound_tag is null"),
                        {"t": r["tag"], "i": r["dest_row"], "p": panel_id, "c": r["tag"].split("_d_", 1)[1]},
                    )
                    check(res.rowcount == 1, f"dest update {r['dest_row']} rowcount {res.rowcount}")
                    res = await conn.execute(
                        text("update hosts set inbound_tag = :t where id = :h and inbound_tag is null and "
                             "workspace_id = :w and tenant_id = :n"),
                        {"t": r["tag"], "h": r["host"], "w": scope["workspace"], "n": scope["tenant"]},
                    )
                    check(res.rowcount == 1, f"host update {r['host']} rowcount {res.rowcount}")
            else:  # rollback
                for r in sorted(rows, key=lambda r: r["dest_row"]):
                    res = await conn.execute(text("update hosts set inbound_tag = null where id = :h and inbound_tag = :t"),
                                             {"h": r["host"], "t": r["tag"]})
                    check(res.rowcount == 1, f"host revert {r['host']}")
                    res = await conn.execute(
                        text("update oc_panel_destination_hosts set virtual_inbound_tag = null where id = :i and "
                             "virtual_inbound_tag = :t"), {"i": r["dest_row"], "t": r["tag"]})
                    check(res.rowcount == 1, f"dest revert {r['dest_row']}")
                    res = await conn.execute(text("delete from inbounds where tag = :t"), {"t": r["tag"]})
                    check(res.rowcount == 1, f"inbound delete {r['tag']}")

            # ------------------------------------------------------------------ post-assertions
            after = await counts(conn)
            check(after == planned, f"count mismatch: expected {planned} got {after}")
            fp_after = await fingerprints(conn, host_ids, dest_ids, tags)
            check(fp_after == fp_before, f"fingerprint of non-approved rows changed: {fp_before} -> {fp_after}")
            rest_after = await row_rest_hashes(conn, host_ids, dest_ids)
            for kind, ids in (("hosts", host_ids), ("dest", dest_ids)):
                for i in ids:
                    check(rest_after[kind][i][0] == rest_before[kind][i][0], f"{kind} {i}: a column other than the tag changed")
            by_dest = {r["dest_row"]: r for r in rows}
            by_host = {r["host"]: r for r in rows}
            want = (lambda r: r["tag"]) if mode == "apply" else (lambda r: None)
            for i, r in by_dest.items():
                check(rest_after["dest"][i][1] == want(r), f"dest {i} tag state wrong")
            for i, r in by_host.items():
                check(rest_after["hosts"][i][1] == want(r), f"host {i} tag state wrong")
            if mode == "apply":
                check(await scalar(conn, "select count(*) from inbounds i join inbounds_groups_association a on "
                                   "a.inbound_id=i.id where i.tag = any(cast(:t as text[]))", t=tags) == 0,
                      "new inbounds unexpectedly have membership rows")
            await trans.commit()
            print("  COMMITTED. all post-assertions passed.")
            print_counts("actual counts", before, after)
            return dict(panel=panel_id, rows=n, before=before, after=after, written=True)
        except Exception:
            if trans.is_active:
                await trans.rollback()
            print(f"  ROLLED BACK panel {panel_id}")
            raise


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--manifest-sha256", required=True, help="pinned sha256 of the manifest")
    ap.add_argument("--panel", required=True, help="panel id, or 'all' (dry run only)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    ap.add_argument("--backup", type=Path, help="fresh backup file (required for --apply/--rollback)")
    ap.add_argument("--backup-sha256", help="expected sha256 of the backup (required for --apply/--rollback)")
    args = ap.parse_args()

    mode = "apply" if args.apply else "rollback" if args.rollback else "dry-run"
    check(sha256_file(args.manifest) == args.manifest_sha256, "manifest sha256 does not match the pinned value")
    if mode != "dry-run":
        check(args.panel != "all", "--apply/--rollback handle exactly one panel per invocation")
        check(args.backup is not None and args.backup_sha256, "--backup and --backup-sha256 are required")
        check(sha256_file(args.backup) == args.backup_sha256, "backup sha256 mismatch")
        age = time.time() - args.backup.stat().st_mtime
        check(age <= MAX_BACKUP_AGE_S, f"backup is {age/60:.0f} min old (limit {MAX_BACKUP_AGE_S//60})")
    panels = list(PANEL_SCOPE) if args.panel == "all" else [int(args.panel)]
    for p in panels:
        check(p in PANEL_SCOPE, f"panel {p} is not in the approved Stage A scope")

    dsn = os.environ.get("STAGEA_DSN")
    check(bool(dsn), "STAGEA_DSN is not set")
    engine = create_async_engine(dsn, connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0})
    totals = {"onlineuser": 0, "moji": 0}
    try:
        for p in panels:
            res = await run_panel(engine, p, args.manifest, mode)
            totals[PANEL_SCOPE[p]["admin"]] += res["rows"]
        if args.panel == "all":
            check(totals == EXPECTED_TOTAL, f"totals {totals} != {EXPECTED_TOTAL}")
            print(f"\nTOTAL planned rows: {totals}  (matches approved 72 + 11)")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Abort as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        sys.exit(2)
