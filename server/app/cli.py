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

    args = parser.parse_args()
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
        session.commit()
    except AuthError as exc:
        session.rollback()
        raise SystemExit(exc.code) from None
    finally:
        session.close()


if __name__ == "__main__":
    main()
