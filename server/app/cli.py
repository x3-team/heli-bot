"""Operator commands. Printed secrets are one-time invite or login codes and training tokens,
never the model key. Run them on the server, do not paste the output into chats or logs."""

import argparse
from decimal import Decimal

from sqlalchemy import select

from app.auth import (
    AuthError,
    create_invite,
    issue_login_code,
    issue_login_code_for_user,
    issue_recovery_code,
    issue_token,
    training_user,
)
from app.db import get_session_factory
from app.models import Base, User
from app.db import get_engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="heli-server")
    sub = parser.add_subparsers(dest="command", required=True)

    invite = sub.add_parser("create-invite")
    invite.add_argument("--max-uses", type=int, default=1)

    code = sub.add_parser("issue-code")
    code.add_argument("--email", required=True)
    code.add_argument("--invite", help="для нового пользователя; для существующего не нужен")

    reset = sub.add_parser("issue-reset-code")
    reset.add_argument("--email", required=True)

    training = sub.add_parser("training-token", help="токен учебного аккаунта из HELI_TRAINING_EMAILS")
    training.add_argument("--email", required=True)

    limit = sub.add_parser("set-limit", help="недельный/месячный лимит пользователя, ₽")
    limit.add_argument("--email", required=True)
    limit.add_argument("--monthly", type=Decimal)
    limit.add_argument("--weekly", type=Decimal)

    wallet = sub.add_parser("wallet-credit", help="кошелёк бота: + оплаченный счёт, − корректировка, ₽")
    wallet.add_argument("--email", required=True)
    wallet.add_argument("--amount", type=Decimal, required=True)
    wallet.add_argument("--note", default="")
    wallet.add_argument("--ref", help="номер платежа/счёта: повтор с тем же номером не зачислится дважды")


    docs = sub.add_parser("crawl-docs", help="проиндексировать официальные справки сервисов (по sitemap)")
    docs.add_argument("--site", action="append", help="только этот сайт, например dev.moysklad.ru")
    docs.add_argument("--limit", type=int, default=100, help="страниц на сайт за запуск")
    check = sub.add_parser("learning-check", help="только чтение: журнал задач, выгрузки, /admin/quality (счётчики)")
    check.add_argument("--days", type=int, default=7)
    check.add_argument("--api", help="адрес запущенного API, например http://127.0.0.1:8001")

    args = parser.parse_args()
    if args.command == "learning-check":
        _learning_check(args.days, args.api)
        return
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    try:
        if args.command == "create-invite":
            print(create_invite(session, max_uses=args.max_uses))
        elif args.command == "issue-code":
            if args.invite:
                print(issue_login_code(session, args.email, args.invite))
            else:
                print(issue_login_code_for_user(session, args.email))
        elif args.command == "issue-reset-code":
            print(issue_recovery_code(session, args.email))
        elif args.command == "training-token":
            user = training_user(session, args.email)
            print(issue_token(user.id, via="training"))
        elif args.command == "crawl-docs":
            from app.search.crawl import crawl, official_sites

            sites = official_sites()
            if args.site:
                wanted = {s.lower().removeprefix("www.") for s in args.site}
                sites = [s for s in sites if s.host.removeprefix("www.") in wanted]
            for report in crawl(sites, per_site=args.limit):
                print(
                    f"{report.site}: sitemap {report.sitemap_urls}, сохранено {report.stored}, "
                    f"свежие {report.skipped_fresh}, robots {report.refused_robots}, ошибки {report.errors}"
                    + (f" ({report.note})" if report.note else "")
                )
        elif args.command == "set-limit":
            user = session.scalar(select(User).where(User.email == args.email.strip().lower()))
            if user is None:
                raise AuthError("user_not_found")
            if args.monthly is not None:
                if args.monthly <= 0:
                    raise SystemExit("monthly must be > 0")
                user.monthly_limit_rub = args.monthly
            if args.weekly is not None:
                user.weekly_limit_rub = args.weekly if args.weekly > 0 else None
            print(f"monthly={user.monthly_limit_rub} weekly={user.weekly_limit_rub or 'auto'}")
        elif args.command == "wallet-credit":
            from app import billing

            user = session.scalar(select(User).where(User.email == args.email.strip().lower()))
            if user is None:
                raise AuthError("user_not_found")
            kind = "topup" if args.amount > 0 else "adjust"
            title = args.note.strip() or ("Пополнение" if kind == "topup" else "Корректировка")
            try:
                billing.post_tx(session, user, kind, args.amount, title, ref=f"admin:{args.ref}" if args.ref else billing.new_ref())
            except billing.BillingError as exc:
                raise SystemExit(exc.code) from None
            print(f"balance={billing.balance(user)}")
        session.commit()
    except AuthError as exc:
        session.rollback()
        raise SystemExit(exc.code) from None
    finally:
        session.close()


def _learning_check(days: int, api: str | None) -> None:
    """Read-only: no create_all, no commit. Prints counts only."""
    import json

    from app.learning_check import learning_check, live_endpoints

    session = get_session_factory()()
    try:
        report = learning_check(session, days=days)
        if api:
            try:
                report["live"] = live_endpoints(session, api, days=days)
            except Exception as exc:  # noqa: BLE001
                report["live"] = {"error": type(exc).__name__}
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    finally:
        session.rollback()
        session.close()


if __name__ == "__main__":
    main()
