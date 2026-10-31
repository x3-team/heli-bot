"""Operator commands. Printed secrets are one-time invite or login codes, never the model key."""

import argparse

from app.auth import AuthError, create_invite, issue_login_code
from app.db import get_session_factory
from app.models import Base
from app.db import get_engine


def main() -> None:
    parser = argparse.ArgumentParser(prog="heli-server")
    sub = parser.add_subparsers(dest="command", required=True)

    invite = sub.add_parser("create-invite")
    invite.add_argument("--max-uses", type=int, default=1)

    code = sub.add_parser("issue-code")
    code.add_argument("--email", required=True)
    code.add_argument("--invite", required=True)

    args = parser.parse_args()
    engine = get_engine()
    Base.metadata.create_all(engine)
    session = get_session_factory()()
    try:
        if args.command == "create-invite":
            print(create_invite(session, max_uses=args.max_uses))
        elif args.command == "issue-code":
            print(issue_login_code(session, args.email, args.invite))
        session.commit()
    except AuthError as exc:
        session.rollback()
        raise SystemExit(exc.code) from None
    finally:
        session.close()


if __name__ == "__main__":
    main()
