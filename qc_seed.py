"""
qc_seed.py
CLI utility untuk Stasiun QC. Seed operator manual karena tidak ada UI registrasi.

Penggunaan:
    python qc_seed.py init-db
    python qc_seed.py add-operator --name "Andi"
    python qc_seed.py add-operator --name "Budi" --supervisor --pin 1234
    python qc_seed.py list-operators
    python qc_seed.py deactivate-operator --name "Andi"
"""

import argparse
import sys
import sqlite3

from qc_stasiun import (
    DB_FILE,
    init_db,
    add_operator,
    list_operators,
    deactivate_operator,
)


def cmd_init_db(_args):
    init_db()
    print(f"OK. Database siap di: {DB_FILE}")


def cmd_add_operator(args):
    if args.supervisor and not args.pin:
        print("ERROR: Supervisor wajib pakai --pin (4 digit atau lebih).")
        sys.exit(1)
    init_db()
    try:
        add_operator(
            name=args.name,
            is_supervisor=bool(args.supervisor),
            pin=args.pin,
        )
        role = "SUPERVISOR" if args.supervisor else "operator"
        print(f"OK. {role} '{args.name}' ditambahkan.")
    except sqlite3.IntegrityError:
        print(f"ERROR: Operator dengan nama '{args.name}' sudah ada.")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def cmd_list_operators(args):
    init_db()
    ops = list_operators(active_only=not args.all)
    if not ops:
        print("(Belum ada operator)")
        return
    print(
        f"{'ID':<4} {'NAMA':<24} {'ROLE':<12} {'AKTIF':<7} {'CREATED':<20}"
    )
    print("-" * 70)
    for op in ops:
        role = "SUPERVISOR" if op["is_supervisor"] else "Operator"
        active = "Ya" if op["is_active"] else "Tidak"
        print(
            f"{op['id']:<4} {op['name']:<24} {role:<12} {active:<7} {str(op['created_at'])[:19]:<20}"
        )


def cmd_deactivate_operator(args):
    init_db()
    if deactivate_operator(args.name):
        print(f"OK. Operator '{args.name}' di-nonaktifkan.")
    else:
        print(f"Operator '{args.name}' tidak ditemukan.")
        sys.exit(1)


def build_parser():
    p = argparse.ArgumentParser(
        prog="qc_seed.py",
        description="Utility CLI untuk seed Stasiun QC.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp_init = sub.add_parser("init-db", help="Buat tabel SQLite di hasil/qc_data.db")
    sp_init.set_defaults(func=cmd_init_db)

    sp_add = sub.add_parser("add-operator", help="Tambah operator QC")
    sp_add.add_argument("--name", required=True, help="Nama operator (unique)")
    sp_add.add_argument(
        "--supervisor", action="store_true", help="Tandai sebagai supervisor"
    )
    sp_add.add_argument(
        "--pin",
        help="PIN supervisor (wajib kalau --supervisor). Disimpan sebagai SHA256 hash.",
    )
    sp_add.set_defaults(func=cmd_add_operator)

    sp_list = sub.add_parser("list-operators", help="Daftar operator")
    sp_list.add_argument(
        "--all", action="store_true", help="Tampilkan juga yang non-aktif"
    )
    sp_list.set_defaults(func=cmd_list_operators)

    sp_deact = sub.add_parser(
        "deactivate-operator", help="Tandai operator non-aktif (soft delete)"
    )
    sp_deact.add_argument("--name", required=True)
    sp_deact.set_defaults(func=cmd_deactivate_operator)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
