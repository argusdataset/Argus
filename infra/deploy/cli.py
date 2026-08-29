"""What a `python -m` entrypoint does with arguments it does not take.

Every process entrypoint in this package — `migrate`, `scanner`,
`retention` — takes no arguments. Each one used to say so like this:

    def main(argv: list[str] | None = None) -> int:
        _ = argv          # discarded

which is correct about the arguments and wrong about what to do with
them. A program that silently ignores its argv cannot tell you when it
was invoked wrongly, and "invoked wrongly" is not hypothetical on a
hosting platform where the start command is a string in a web form.

## The failure this exists to prevent, which already happened

A Railway service was configured with:

    python -m infra.deploy.migrate && uvicorn infra.deploy.asgi:identity_app ...

If that string reaches a shell, `&&` sequences two commands and both
run. If it is instead exec'd directly — no shell — then `&&`, `uvicorn`
and every flag after it become **argv for the migration**, which
discarded them, migrated nothing, printed "schema already at head",
and exited 0. The platform saw a successful process that ended, no
server was ever started, and the health check failed five minutes later
against a port nothing was listening on.

Every observable was consistent with success. The one thing that would
have named the problem instantly — "I take no arguments, and I was given
`['&&', 'uvicorn', ...]`" — was the thing `_ = argv` threw away.

So: refuse, name what was passed, and exit non-zero. An entrypoint that
cannot use its arguments should not pretend it received none.
"""

from __future__ import annotations

import sys

__all__ = ["UnexpectedArguments", "refuse_arguments"]


class UnexpectedArguments(SystemExit):
    """Raised as a `SystemExit`, so `python -m` exits non-zero on its own.

    `SystemExit` rather than a bespoke exception because these are
    entrypoints: there is no caller to catch this, and the correct
    behaviour is for the process to stop with a message and a non-zero
    code. Subclassed so a test can assert on the type.
    """


def refuse_arguments(module: str, argv: list[str] | None) -> None:
    """Stop with a diagnosis if this entrypoint was given arguments.

    `argv` is `None` when called in-process (a test, an import) and a
    list when a `__main__` block passes `sys.argv[1:]`. An empty list is
    the normal case and passes.

    The message names the arguments verbatim, because the whole point is
    that seeing them is the diagnosis. It also calls out the `&&` case
    specifically — not to be clever, but because that is the one that
    actually happened, and because a person reading a deploy log at the
    end of a long afternoon should not have to infer it.
    """
    if not argv:
        return

    joined = " ".join(repr(item) for item in argv)
    hint = ""
    if any(item in {"&&", "||", ";", "|"} for item in argv):
        hint = (
            "\n\nThat argument list begins with a shell operator, which means this "
            "command was executed WITHOUT a shell — so the operator and everything "
            "after it were handed to this program as arguments instead of starting a "
            "second command. Nothing after the operator ran. Wrap the whole thing so a "
            "shell definitely sees it:\n\n"
            f"    sh -c 'python -m {module} && <the next command>'"
        )

    message = (
        f"python -m {module} takes no arguments, and received: {joined}{hint}\n\n"
        "Refusing rather than ignoring them: an entrypoint that silently discards "
        "its argv cannot tell you it was invoked wrongly, and this one is started "
        "from a string in a deployment platform's configuration field."
    )
    print(f"argus.entrypoint: {message}", file=sys.stderr, flush=True)
    raise UnexpectedArguments(2)
